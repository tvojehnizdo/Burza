$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Kraken PRIVATE readiness - VALIDATE ONLY" -ForegroundColor Cyan
Write-Host "No live order will be submitted. Withdraw permission must be disabled." -ForegroundColor Yellow

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -m venv .venv
}
Set-ExecutionPolicy -Scope Process Bypass -Force
& ".\.venv\Scripts\Activate.ps1"
python -m pip install -r requirements.txt

$candidates = @(
    @(
        $env:KRAKEN_ENV_FILE,
        "C:\TvojeHnizdo\Vault\.env",
        (Join-Path $PSScriptRoot ".env")
    ) | Where-Object { $_ -and (Test-Path $_) }
)

if ($candidates.Count -gt 0) {
    $envFile = [string]$candidates[0]
} else {
    $envFile = Read-Host "Path to your .env file containing the Kraken key/secret"
    if (-not (Test-Path $envFile)) {
        throw "Env file not found: $envFile"
    }
}

Write-Host "Credential file found. Values will not be printed." -ForegroundColor Green
$env:KRAKEN_ENV_FILE = $envFile

python kraken_private.py --env-file "$envFile" --json
$readinessExit = $LASTEXITCODE
if ($readinessExit -ne 0) {
    Write-Host ""
    Write-Host "Readiness failed. Sanitized env inspection follows (variable names only; no values)." -ForegroundColor Yellow
    python kraken_private.py --env-file "$envFile" --inspect-env
    throw "Kraken readiness failed with exit code $readinessExit"
}

Write-Host ""
Write-Host "If safe_to_arm=true and actual_order_submitted=false, private API is ready without a real trade." -ForegroundColor Green
