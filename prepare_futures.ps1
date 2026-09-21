$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Store = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Save-Store([Security.SecureString]$Key, [Security.SecureString]$Secret) {
    $dir = Split-Path $Store -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [ordered]@{
        version = 1
        created_at = (Get-Date).ToString("o")
        api_key = ConvertFrom-SecureString $Key
        api_secret = ConvertFrom-SecureString $Secret
        protection = "Windows DPAPI / current user"
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $Store
}

Write-Host ""
Write-Host "KRAKEN FUTURES API SETUP" -ForegroundColor Cyan
Write-Host "Pro plné obchodování s futures nastav:" -ForegroundColor Yellow
Write-Host "  General API: FULL ACCESS"
Write-Host "  Transfer/Withdrawal API: NO ACCESS"
Write-Host ""
Write-Host "To umožní obchodovat futures, ale nepovolí převody/výběry přes tento klíč." -ForegroundColor Green
Write-Host ""

$have = Read-Host "Máš už vytvořený Futures API key + secret? [A/n]"
if ($have.Trim().ToUpperInvariant() -eq "N") {
    Start-Process "https://pro.kraken.com/app/settings/api"
    Write-Host "V dolní části vytvoř samostatný Futures API key." -ForegroundColor Cyan
    Write-Host "General API = Full Access, Transfer/Withdrawal = No Access." -ForegroundColor Cyan
    Read-Host "Až bude key vytvořený a secret zobrazený, stiskni ENTER"
}

$key = Read-Host "Kraken FUTURES API key" -AsSecureString
$secret = Read-Host "Kraken FUTURES API secret" -AsSecureString
if ((Secure-ToPlain $key).Length -lt 8 -or (Secure-ToPlain $secret).Length -lt 16) {
    throw "Futures key/secret vypadá neúplně."
}

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $key
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $secret

& $Python futures_private.py --readiness
if ($LASTEXITCODE -ne 0) {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
    throw "Futures API readiness selhalo."
}

$save = Read-Host "Uložit Futures key+secret lokálně šifrovaně přes Windows DPAPI? [A/n]"
if ([string]::IsNullOrWhiteSpace($save) -or $save.Trim().ToUpperInvariant() -in @("A","Y")) {
    Save-Store $key $secret
    Write-Host "Uloženo: $Store" -ForegroundColor Green
}

Write-Host ""
Write-Host "Futures API ověřeno. LIVE futures zůstává záměrně vypnuté v policy." -ForegroundColor Green

Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
