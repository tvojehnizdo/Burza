$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Store = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
    Write-Host ""
    Write-Host "FUTURES CANARY - ONE SHOT AUTO WATCH" -ForegroundColor Cyan
    Write-Host "Max 1 LIVE pozice, hard cap 3 USD, pouze XBT/ETH/SOL." -ForegroundColor Yellow
    Write-Host "Po vstupu: reduce-only STOP + TAKE PROFIT. Pokud ochrana selze, executor zkusi okamzite zplosteni." -ForegroundColor Yellow
    Write-Host ""

    $arm = Read-Host "Pro jednorazove ozbrojeni a cekani na prvni executable signal napis presne ARM FUTURES CANARY"
    if ($arm -ne "ARM FUTURES CANARY") {
        Write-Host "Neozbrojeno. Nic LIVE se neposlalo." -ForegroundColor Yellow
        exit 1
    }

    $deadline = (Get-Date).AddMinutes(20)
    $attempt = 0

    while ((Get-Date) -lt $deadline) {
        $attempt++
        $planJson = & $Python futures_canary.py --plan
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
            Write-Host ("FOUND #" + $attempt + ": " + $plan.candidate.symbol + " " + $plan.candidate.side +
                " | notional ~$" + [math]::Round([double]$plan.candidate.estimated_notional_usd, 2) +
                " | edge=" + [math]::Round([double]$plan.candidate.taker_net_edge_bps, 2) + " bps" +
                " | stop=" + [math]::Round([double]$plan.candidate.stop_price, 4) +
                " | take=" + [math]::Round([double]$plan.candidate.take_profit_price, 4)) -ForegroundColor Green

            $execJson = & $Python futures_canary.py --execute --confirm SPUSTIT-FUTURES-CANARY
            $execJson | Write-Host
            if ($LASTEXITCODE -ne 0) { throw "Futures canary execute selhal." }

            try {
                $exec = ($execJson -join [Environment]::NewLine) | ConvertFrom-Json
            }
            catch {
                throw "Execute vratil necitelny JSON."
            }

            if ($exec.actual_order_submitted) {
                Write-Host ""
                Write-Host "LIVE CANARY ODESLAN. Dalsi obchod se v tomto behu neposle." -ForegroundColor Green
                exit 0
            }

            Write-Host ("Signal pri execute zmizel nebo nebyl proveditelny: " + [string]$exec.reason + ". Pokracuji v cekani.") -ForegroundColor Yellow
        }
        else {
            $best = $plan.public_scan.candidate
            if ($best) {
                Write-Host ("[" + (Get-Date -Format "HH:mm:ss") + "] cekam | reason=" + [string]$plan.reason +
                    " | best=" + [string]$best.symbol +
                    " " + [string]$best.side +
                    " | edge=" + [math]::Round([double]($best.taker_net_edge_bps), 2) + " bps") -ForegroundColor DarkGray
            }
            else {
                Write-Host ("[" + (Get-Date -Format "HH:mm:ss") + "] cekam | reason=" + [string]$plan.reason) -ForegroundColor DarkGray
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
