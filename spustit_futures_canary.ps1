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
    Write-Host ""
    Write-Host "FUTURES MICRO PORTFOLIO" -ForegroundColor Cyan
    Write-Host "Max 4 ruzne pozice | cil ~2.25 USD | max 3 USD/pozice | portfolio max 10 USD / 50 % equity." -ForegroundColor Yellow
    Write-Host "Kazdy vstup dostane reduce-only STOP + TAKE PROFIT." -ForegroundColor Yellow
    Write-Host ""

    $rescueJson = & $Python "futures_canary.py" --rescue
    if ($LASTEXITCODE -ne 0) { throw "Rescue kontrola selhala." }
    $rescueJson | Write-Host
    $rescue = ($rescueJson -join [Environment]::NewLine) | ConvertFrom-Json

    if ([string]$rescue.reason -eq "TOO_MANY_EXISTING_POSITIONS" -or [string]$rescue.reason -eq "RESCUE_FLATTEN_FAILED") {
        throw ("Rescue stop: " + [string]$rescue.reason)
    }

    $deadline = (Get-Date).AddMinutes(20)

    while ((Get-Date) -lt $deadline) {
        $planJson = & $Python "futures_canary.py" --plan
        if ($LASTEXITCODE -ne 0) {
            Start-Sleep -Seconds 5
            continue
        }

        $plan = ($planJson -join [Environment]::NewLine) | ConvertFrom-Json
        $openCount = 0
        try { $openCount = [int]$plan.readiness.open_position_count } catch { $openCount = 0 }

        if ([string]$plan.reason -eq "POSITION_SLOTS_FULL" -or $openCount -ge 4) {
            Write-Host ("Portfolio naplneno: " + $openCount + "/4. Ochranny ordery zustavaji na burze.") -ForegroundColor Green
            exit 0
        }

        if ($plan.ready -and [string]$plan.reason -eq "FUTURES_CANARY_EXECUTABLE") {
            Write-Host ""
            Write-Host (
                "KANDIDAT: " + [string]$plan.candidate.symbol +
                " " + [string]$plan.candidate.side +
                " | notional ~$" + [math]::Round([double]$plan.candidate.estimated_notional_usd, 2) +
                " | edge=" + [math]::Round([double]$plan.candidate.taker_net_edge_bps, 2) + " bps" +
                " | stop=" + [math]::Round([double]$plan.candidate.stop_price, 6) +
                " | take=" + [math]::Round([double]$plan.candidate.take_profit_price, 6)
            ) -ForegroundColor Green

            $confirm = Read-Host "Pro tento dalsi LIVE mikro obchod napis presne SPUSTIT MICRO OBCHOD"
            if ($confirm -ne "SPUSTIT MICRO OBCHOD") {
                Write-Host "Tento vstup preskocen. Skript konci bez noveho orderu." -ForegroundColor Yellow
                exit 1
            }

            $execJson = & $Python "futures_canary.py" --execute --confirm SPUSTIT-FUTURES-CANARY
            if ($LASTEXITCODE -ne 0) { throw "Execute selhal." }
            $execJson | Write-Host
            $execObj = ($execJson -join [Environment]::NewLine) | ConvertFrom-Json

            if ($execObj.ok -and [string]$execObj.reason -eq "FUTURES_CANARY_LIVE_WITH_PROTECTION") {
                Write-Host ("LIVE + OCHRANA OK: " + [string]$execObj.candidate.symbol) -ForegroundColor Green
                Start-Sleep -Seconds 2
                continue
            }

            Write-Host ("Novy trade nebyl ponechan otevreny: " + [string]$execObj.reason) -ForegroundColor Yellow
            Start-Sleep -Seconds 5
            continue
        }

        $best = $plan.public_scan.candidate
        $msg = "[" + (Get-Date -Format "HH:mm:ss") + "] " + [string]$plan.reason + " | open=" + $openCount + "/4"
        if ($null -ne $best) {
            $edge = 0.0
            try { $edge = [double]$best.taker_net_edge_bps } catch { $edge = 0.0 }
            $msg += " | best=" + [string]$best.symbol + " " + [string]$best.side + " edge=" + [math]::Round($edge, 2) + "bps"
        }
        Write-Host $msg -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }

    Write-Host "20 minut bez dalsiho potvrzeneho executable vstupu. Koncim; otevrene ochrany zustavaji na burze." -ForegroundColor Yellow
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
