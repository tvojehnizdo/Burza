$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Store = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Load-EncryptedPair([string]$Path) {
    if (-not (Test-Path $Path)) {
        return $null
    }

    $obj = Get-Content -Raw $Path | ConvertFrom-Json
    return @(
        (ConvertTo-SecureString -String ([string]$obj.api_key)),
        (ConvertTo-SecureString -String ([string]$obj.api_secret))
    )
}

if (-not (Test-Path $Python)) {
    throw "Python venv chybi. Spust start_v4_neutral.ps1."
}

$pair = Load-EncryptedPair $Store
if (-not $pair) {
    throw "Kraken Futures DPAPI credentials chybi: $Store. Spust prepare_futures.ps1."
}

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $pair[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $pair[1]

try {
    Write-Host ""
    Write-Host "FUTURES CANARY - ONE SHOT AUTO WATCH" -ForegroundColor Cyan
    Write-Host "Max 1 LIVE pozice, hard cap 3 USD, dynamicky Kraken PF_*USD perpetual universe." -ForegroundColor Yellow
    Write-Host "Po vstupu: reduce-only STOP + TAKE PROFIT." -ForegroundColor Yellow
    Write-Host "Pokud ochrana selze, executor zkusi okamzite zplosteni." -ForegroundColor Yellow
    Write-Host ""

    $arm = Read-Host "Pro jednorazove ozbrojeni napis presne ARM FUTURES CANARY"
    if ($arm -ne "ARM FUTURES CANARY") {
        Write-Host "Neozbrojeno. Nic LIVE se neposlalo." -ForegroundColor Yellow
        exit 1
    }

    Write-Host ""
    Write-Host "Kontroluji existujici Futures pozici a ochranu..." -ForegroundColor Cyan
    $rescueJson = & $Python "futures_canary.py" --rescue
    if ($LASTEXITCODE -ne 0) {
        throw "Rescue kontrola selhala."
    }
    $rescueJson | Write-Host

    try {
        $rescue = ($rescueJson -join [Environment]::NewLine) | ConvertFrom-Json
    }
    catch {
        throw "Rescue vratil necitelny JSON."
    }

    if ([string]$rescue.reason -eq "EXISTING_POSITION_PROTECTED") {
        Write-Host ""
        Write-Host ("Existujici pozice " + [string]$rescue.symbol + " je nyni chranena STOP + TAKE PROFIT.") -ForegroundColor Green
        Write-Host "Dalsi pozici neoteviram, dokud tato existuje." -ForegroundColor Green
        exit 0
    }

    if ([string]$rescue.reason -eq "MULTIPLE_EXISTING_POSITIONS" -or [string]$rescue.reason -eq "RESCUE_FLATTEN_FAILED") {
        Write-Host ("STOP: rescue reason=" + [string]$rescue.reason) -ForegroundColor Red
        exit 2
    }

    if ([string]$rescue.reason -eq "EXISTING_POSITION_FLATTENED") {
        Write-Host "Stara nekompletne chranena pozice byla zplostena. Pokracuji do noveho signalu." -ForegroundColor Yellow
    }

    $deadline = (Get-Date).AddMinutes(20)
    $attempt = 0

    while ((Get-Date) -lt $deadline) {
        $attempt++

        $planJson = & $Python "futures_canary.py" --plan
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Plan selhal, opakuji za 5 s..." -ForegroundColor Yellow
            Start-Sleep -Seconds 5
            continue
        }

        try {
            $plan = ($planJson -join [Environment]::NewLine) | ConvertFrom-Json
        }
        catch {
            Write-Host "Plan JSON nesel precist, opakuji za 5 s..." -ForegroundColor Yellow
            Start-Sleep -Seconds 5
            continue
        }

        if ([string]$plan.reason -eq "EXISTING_FUTURES_POSITION") {
            Write-Host "STOP: uz existuje futures pozice. Nic dalsiho neposilam." -ForegroundColor Yellow
            exit 2
        }

        if ($plan.ready -and [string]$plan.reason -eq "FUTURES_CANARY_EXECUTABLE") {
            Write-Host ""
            Write-Host (
                "FOUND #" + $attempt +
                ": " + [string]$plan.candidate.symbol +
                " " + [string]$plan.candidate.side +
                " | notional ~$" + [math]::Round([double]$plan.candidate.estimated_notional_usd, 2) +
                " | edge=" + [math]::Round([double]$plan.candidate.taker_net_edge_bps, 2) + " bps" +
                " | stop=" + [math]::Round([double]$plan.candidate.stop_price, 4) +
                " | take=" + [math]::Round([double]$plan.candidate.take_profit_price, 4)
            ) -ForegroundColor Green

            $execJson = & $Python "futures_canary.py" --execute --confirm SPUSTIT-FUTURES-CANARY
            if ($LASTEXITCODE -ne 0) {
                throw "Futures canary execute selhal."
            }

            $execJson | Write-Host

            try {
                $execObj = ($execJson -join [Environment]::NewLine) | ConvertFrom-Json
            }
            catch {
                throw "Execute vratil necitelny JSON."
            }

            if ($execObj.actual_order_submitted) {
                Write-Host ""
                Write-Host ("LIVE CANARY ODESLAN. reason=" + [string]$execObj.reason) -ForegroundColor Green
                Write-Host "Dalsi obchod se v tomto behu neposle." -ForegroundColor Green
                exit 0
            }

            Write-Host (
                "Signal pri execute zmizel nebo nebyl proveditelny: " +
                [string]$execObj.reason +
                ". Pokracuji v cekani."
            ) -ForegroundColor Yellow
        }
        else {
            $best = $plan.public_scan.candidate

            if ($null -ne $best) {
                $edge = 0.0
                try {
                    $edge = [double]$best.taker_net_edge_bps
                }
                catch {
                    $edge = 0.0
                }

                Write-Host (
                    "[" + (Get-Date -Format "HH:mm:ss") + "] cekam" +
                    " | reason=" + [string]$plan.reason +
                    " | best=" + [string]$best.symbol +
                    " " + [string]$best.side +
                    " | edge=" + [math]::Round($edge, 2) + " bps"
                ) -ForegroundColor DarkGray
            }
            else {
                Write-Host (
                    "[" + (Get-Date -Format "HH:mm:ss") + "] cekam" +
                    " | reason=" + [string]$plan.reason
                ) -ForegroundColor DarkGray
            }
        }

        Start-Sleep -Seconds 5
    }

    Write-Host "20 minut bez executable signalu. Nic LIVE se neposlalo." -ForegroundColor Yellow
    exit 3
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}
