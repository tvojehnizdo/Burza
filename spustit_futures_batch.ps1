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
    Write-Host "FUTURES LIVE BATCH" -ForegroundColor Cyan
    Write-Host "Jedno potvrzeni -> az 4 soucasne mikro pozice." -ForegroundColor Yellow
    Write-Host "Max 3 USD/obchod | portfolio cap 10 USD / 50 % equity." -ForegroundColor Yellow
    Write-Host "Po otevreni prevezme pozice autonomni manager." -ForegroundColor Yellow
    Write-Host ""

    $deadline = (Get-Date).AddMinutes(20)
    $plan = $null

    while ((Get-Date) -lt $deadline) {
        $planJson = & $Python "futures_canary.py" --plan
        if ($LASTEXITCODE -ne 0) {
            Start-Sleep -Seconds 5
            continue
        }

        try { $plan = ($planJson -join [Environment]::NewLine) | ConvertFrom-Json }
        catch {
            Start-Sleep -Seconds 5
            continue
        }

        if ($plan.ready -and [string]$plan.reason -eq "FUTURES_CANARY_EXECUTABLE") { break }

        $best = $plan.public_scan.candidate
        if ($null -ne $best) {
            Write-Host (
                "[" + (Get-Date -Format "HH:mm:ss") + "] cekam" +
                " | best=" + [string]$best.symbol +
                " " + [string]$best.side +
                " | edge=" + [math]::Round([double]$best.taker_net_edge_bps, 2) + "bps"
            ) -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 5
    }

    if ($null -eq $plan -or -not $plan.ready) {
        Write-Host "Nebyl nalezen executable kandidat. Nic LIVE se neposlalo." -ForegroundColor Yellow
        exit 2
    }

    $openCount = [int]$plan.readiness.open_position_count
    $slots = [math]::Max(0, 4 - $openCount)
    if ($slots -le 0) {
        Write-Host "Portfolio ma 4/4 pozice. Zadny novy batch se neotevre." -ForegroundColor Yellow
        exit 0
    }

    $candidates = @()
    if ($null -ne $plan.candidate) { $candidates += $plan.candidate }
    if ($null -ne $plan.alternatives) { $candidates += @($plan.alternatives) }
    $preview = @($candidates | Select-Object -First $slots)

    Write-Host ""
    Write-Host ("Aktualni batch kandidatů: " + $preview.Count + " | volne sloty: " + $slots) -ForegroundColor Green
    foreach ($x in $preview) {
        Write-Host (
            "  " + [string]$x.symbol +
            " " + [string]$x.side +
            " | ~$" + [math]::Round([double]$x.estimated_notional_usd, 2) +
            " | edge=" + [math]::Round([double]$x.taker_net_edge_bps, 2) + "bps" +
            " | stop=" + [math]::Round([double]$x.stop_price, 6) +
            " | take=" + [math]::Round([double]$x.take_profit_price, 6)
        ) -ForegroundColor Green
    }

    $confirm = Read-Host "Pro schvaleni celeho batch napiš presne SPUSTIT LIVE BATCH"
    if ($confirm -ne "SPUSTIT LIVE BATCH") {
        Write-Host "Batch nebyl schvalen. Nic noveho LIVE se neposlalo." -ForegroundColor Yellow
        exit 1
    }

    $opened = 0
    for ($i = 0; $i -lt $slots; $i++) {
        $execJson = & $Python "futures_canary.py" --execute --confirm SPUSTIT-FUTURES-CANARY
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Execute selhal; dalsi vstupy zastavuji." -ForegroundColor Yellow
            break
        }

        $execJson | Write-Host
        try { $obj = ($execJson -join [Environment]::NewLine) | ConvertFrom-Json }
        catch { break }

        if ($obj.ok -and [string]$obj.reason -eq "FUTURES_CANARY_LIVE_WITH_PROTECTION") {
            $opened++
            Write-Host ("BATCH OPEN " + $opened + "/" + $slots + ": " + [string]$obj.candidate.symbol) -ForegroundColor Green
            Start-Sleep -Seconds 2
            continue
        }

        if ([string]$obj.reason -eq "POSITION_SLOTS_FULL" -or [string]$obj.reason -eq "PORTFOLIO_NOTIONAL_FULL") {
            break
        }

        Start-Sleep -Seconds 2
    }

    Write-Host ""
    Write-Host ("Batch dokoncen. Novych LIVE pozic: " + $opened) -ForegroundColor Green
    Write-Host "Spoustim autonomni spravu otevrenych pozic..." -ForegroundColor Cyan

    & $Python "futures_autopilot.py" --run --confirm ARM-LIVE-MANAGER
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
