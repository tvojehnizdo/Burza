$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$KrakenStore = "C:\TvojeHnizdo\Vault\Kraken\trading.credentials.dpapi.json"
$OpenAIStore = "C:\TvojeHnizdo\Vault\OpenAI\impulse-supervisor.dpapi.json"

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Load-EncryptedPair([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    $obj = Get-Content -Raw $Path | ConvertFrom-Json
    return @(
        ConvertTo-SecureString ([string]$obj.api_key),
        ConvertTo-SecureString ([string]$obj.api_secret)
    )
}

function Save-OpenAIKey([Security.SecureString]$Key) {
    $dir = Split-Path $OpenAIStore -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [ordered]@{
        version = 1
        created_at = (Get-Date).ToString("o")
        openai_api_key = ConvertFrom-SecureString $Key
        protection = "Windows DPAPI / current user"
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $OpenAIStore
}

function Load-OpenAIKey {
    if (-not (Test-Path $OpenAIStore)) { return $null }
    try {
        $obj = Get-Content -Raw $OpenAIStore | ConvertFrom-Json
        return ConvertTo-SecureString ([string]$obj.openai_api_key)
    } catch { return $null }
}

if (-not (Test-Path $Python)) {
    py -m venv .venv
}
Set-ExecutionPolicy -Scope Process Bypass -Force
& ".\.venv\Scripts\Activate.ps1"
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }

if (-not (Test-Path $KrakenStore)) {
    Write-Host "Kraken encrypted credentials are missing. Running complete setup..." -ForegroundColor Yellow
    & ".\spustit_vse.ps1"
}
$kp = Load-EncryptedPair $KrakenStore
if (-not $kp) { throw "Unable to load Kraken encrypted credentials." }
$env:KRAKEN_API_KEY = Secure-ToPlain $kp[0]
$env:KRAKEN_API_SECRET = Secure-ToPlain $kp[1]

$oa = Load-OpenAIKey
if (-not $oa) {
    Write-Host ""
    Write-Host "OpenAI API key pro lokální AI Supervisor není uložen." -ForegroundColor Yellow
    Write-Host "Vlož ho pouze sem do PowerShellu; do chatu ho neposílej." -ForegroundColor Yellow
    $oa = Read-Host "OPENAI_API_KEY" -AsSecureString
    if ((Secure-ToPlain $oa).Length -lt 20) { throw "OpenAI API key looks incomplete." }
    $save = Read-Host "Uložit OpenAI key lokálně šifrovaně přes Windows DPAPI? [A/n]"
    if ([string]::IsNullOrWhiteSpace($save) -or $save.Trim().ToUpperInvariant() -in @("A","Y")) {
        Save-OpenAIKey $oa
        Write-Host "OpenAI key uložen šifrovaně: $OpenAIStore" -ForegroundColor Green
    }
}
$env:OPENAI_API_KEY = Secure-ToPlain $oa

if (-not $env:OPENAI_SUPERVISOR_MODEL) { $env:OPENAI_SUPERVISOR_MODEL = "gpt-5.6-terra" }
if (-not $env:SUPERVISOR_INTERVAL_S) { $env:SUPERVISOR_INTERVAL_S = "300" }
if (-not $env:SUPERVISOR_CONTROL) { $env:SUPERVISOR_CONTROL = "1" }
if (-not $env:SUPERVISOR_AUTO) { $env:SUPERVISOR_AUTO = "1" }

Write-Host ""
Write-Host "IMPULSE AI Supervisor starting..." -ForegroundColor Cyan
Write-Host "Dashboard: http://127.0.0.1:8770" -ForegroundColor Green
Write-Host "AUTO interval: $env:SUPERVISOR_INTERVAL_S s" -ForegroundColor Green
Write-Host "AI cannot enable LIVE, withdrawals or wallet transfers." -ForegroundColor Yellow

Start-Process "http://127.0.0.1:8770"
& $Python -m uvicorn ai_supervisor:app --host 127.0.0.1 --port 8770

Remove-Item Env:KRAKEN_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:KRAKEN_API_SECRET -ErrorAction SilentlyContinue
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
