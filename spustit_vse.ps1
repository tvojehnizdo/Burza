param(
    [switch]$ConsolidateUSDC
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Reports = Join-Path $PSScriptRoot "reports"
$VaultRoot = "C:\TvojeHnizdo\Vault"
if (-not (Test-Path $VaultRoot)) {
    $VaultRoot = Join-Path $env:LOCALAPPDATA "ImpulseMax5K\Vault"
}
$KrakenVault = Join-Path $VaultRoot "Kraken"
$CredStore = Join-Path $KrakenVault "trading.credentials.dpapi.json"
$LegacyVault = "C:\TvojeHnizdo\Vault\.env"
$ReadinessJson = Join-Path $Reports "kraken-readiness-latest.json"
$ReadinessErr = Join-Path $Reports "kraken-readiness-latest.err.txt"

function Banner([string]$Text, [ConsoleColor]$Color = [ConsoleColor]::Cyan) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkGray
    Write-Host $Text -ForegroundColor $Color
    Write-Host ("=" * 72) -ForegroundColor DarkGray
}

function Text([object]$Value) {
    if ($null -eq $Value) { return "" }
    return [string]$Value
}

function ChoiceUpper([object]$Value) {
    return (Text $Value).Trim().ToUpperInvariant()
}

function Secure-ToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Protect-CredentialStore([Security.SecureString]$Key, [Security.SecureString]$Secret) {
    New-Item -ItemType Directory -Force -Path $KrakenVault | Out-Null
    $obj = [ordered]@{
        version = 1
        created_at = (Get-Date).ToString("o")
        api_key = ConvertFrom-SecureString $Key
        api_secret = ConvertFrom-SecureString $Secret
        protection = "Windows DPAPI / current user"
    }
    $obj | ConvertTo-Json | Set-Content -Encoding UTF8 $CredStore

    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $CredStore /inheritance:r /grant:r (($identity) + ":(F)") /grant:r "SYSTEM:(F)" | Out-Null
    }
    catch {
        Write-Host "Pozn.: ACL souboru se nepodařilo zpřísnit, DPAPI šifrování ale zůstává aktivní." -ForegroundColor Yellow
    }
}

function Load-ProtectedCredentialStore {
    if (-not (Test-Path $CredStore)) { return $null }
    try {
        $obj = Get-Content -Raw $CredStore | ConvertFrom-Json
        $key = ConvertTo-SecureString ([string]$obj.api_key)
        $secret = ConvertTo-SecureString ([string]$obj.api_secret)
        return @($key, $secret)
    }
    catch {
        Write-Host "Uložené šifrované přihlašovací údaje nelze načíst. Budu se ptát znovu." -ForegroundColor Yellow
        return $null
    }
}

function Prompt-Credentials {
    while ($true) {
        Banner "DOPLNĚNÍ KRAKEN API ÚDAJŮ" Yellow
        Write-Host "Potřebuji pouze API KEY + API SECRET. Žádný samostatný WebSocket token nevkládáš."
        Write-Host "WebSocket token si systém vyžádá automaticky z Kraken API, pokud má key oprávnění WebSocket interface."
        Write-Host ""
        Write-Host "Vkládej údaje pouze sem do PowerShellu, nikdy do chatu." -ForegroundColor Yellow
        Write-Host "Vstup je skrytý a hodnoty se nevypisují."
        Write-Host ""
        Write-Host "Pokud key ještě nemáš, otevřu Kraken API nastavení." -ForegroundColor Cyan
        $open = Read-Host "Máš nyní Kraken API key + secret? [A/n]"
        if ((ChoiceUpper $open) -eq "N") {
            try { Start-Process "https://pro.kraken.com/app/settings/api" } catch { }
            Write-Host ""
            Write-Host "Vytvoř key a nastav:" -ForegroundColor Cyan
            Write-Host "  ON  Query Funds"
            Write-Host "  ON  Query Open Orders & Trades"
            Write-Host "  ON  Create & Modify Orders"
            Write-Host "  ON  Cancel & Close Orders"
            Write-Host "  ON  WebSocket interface"
            Write-Host "  OFF Withdraw Funds"
            Write-Host "  OFF Add Withdrawal Addresses"
            Write-Host "  OFF Update Withdrawal Addresses"
            Write-Host ""
            Read-Host "Až bude key vytvořený a secret zobrazený, stiskni ENTER"
        }

        $key = Read-Host "Kraken API key / public key" -AsSecureString
        $secret = Read-Host "Kraken API secret / private key" -AsSecureString
        $keyPlain = Secure-ToPlain $key
        $secretPlain = Secure-ToPlain $secret

        if ($keyPlain.Length -lt 8) {
            Write-Host "API key je příliš krátký/neúplný. Zkus znovu." -ForegroundColor Red
            continue
        }
        if ($secretPlain.Length -lt 16) {
            Write-Host "API secret je příliš krátký/neúplný. Zkus znovu." -ForegroundColor Red
            continue
        }

        $save = Read-Host "Uložit oba údaje lokálně šifrovaně přes Windows DPAPI pro další spuštění? [A/n]"
        if ([string]::IsNullOrWhiteSpace((Text $save)) -or (ChoiceUpper $save) -in @("A","Y")) {
            Protect-CredentialStore $key $secret
            Write-Host "Uloženo šifrovaně: $CredStore" -ForegroundColor Green
        }
        return @($key, $secret)
    }
}

function Set-Process-Credentials([Security.SecureString]$Key, [Security.SecureString]$Secret) {
    $env:KRAKEN_API_KEY = Secure-ToPlain $Key
    $env:KRAKEN_API_SECRET = Secure-ToPlain $Secret
}

function Clear-Process-Credentials {
    Remove-Item Env:KRAKEN_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:KRAKEN_API_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:KRAKEN_OTP -ErrorAction SilentlyContinue
}

function Invoke-Readiness([string]$EnvFile = "") {
    New-Item -ItemType Directory -Force -Path $Reports | Out-Null
    Remove-Item $ReadinessJson,$ReadinessErr -Force -ErrorAction SilentlyContinue

    if ($EnvFile) {
        & $Python "kraken_private.py" --env-file $EnvFile --json 1> $ReadinessJson 2> $ReadinessErr
    }
    else {
        & $Python "kraken_private.py" --json 1> $ReadinessJson 2> $ReadinessErr
    }
    $exit = $LASTEXITCODE
    $report = $null
    if (Test-Path $ReadinessJson) {
        $raw = Get-Content -Raw $ReadinessJson -ErrorAction SilentlyContinue
        if (-not [string]::IsNullOrWhiteSpace((Text $raw))) {
            try { $report = (Text $raw) | ConvertFrom-Json } catch { }
        }
    }
    $err = ""
    if (Test-Path $ReadinessErr) {
        $tmpErr = Get-Content -Raw $ReadinessErr -ErrorAction SilentlyContinue
        if ($null -ne $tmpErr) { $err = [string]$tmpErr }
    }

    return [pscustomobject]@{
        ExitCode = $exit
        Report = $report
        ErrorText = $err
    }
}

function Show-Permission-Checklist($Report) {
    Banner "KRAKEN API OPRÁVNĚNÍ K DOPLNĚNÍ" Yellow
    Write-Host "Na Kraken Pro otevři: profil -> Settings -> API -> použitý trading key."
    Write-Host ""
    Write-Host "Musí být ZAPNUTO:" -ForegroundColor Cyan
    Write-Host "  [ON]  Query Funds"
    Write-Host "  [ON]  Query Open Orders & Trades"
    Write-Host "  [ON]  Create & Modify Orders / Modify Orders"
    Write-Host "  [ON]  Cancel & Close Orders"
    Write-Host "  [ON]  WebSocket interface  (systém z něj získá token automaticky)"
    Write-Host ""
    Write-Host "Musí být VYPNUTO:" -ForegroundColor Cyan
    Write-Host "  [OFF] Withdraw Funds"
    Write-Host "  [OFF] Add Withdrawal Addresses"
    Write-Host "  [OFF] Update Withdrawal Addresses"
    Write-Host ""

    if ($Report -and $Report.checks) {
        Write-Host "Aktuálně neprošlo:" -ForegroundColor Yellow
        foreach ($p in $Report.checks.PSObject.Properties) {
            if ($p.Value -eq $false) { Write-Host ("  - " + $p.Name) -ForegroundColor Red }
        }
    }

    try {
        Start-Process "https://pro.kraken.com/app/settings/api"
    } catch { }

    Write-Host ""
    Write-Host "Pokud Kraken neumí oprávnění existujícího klíče změnit, vytvoř nový key a zvol stejné minimum oprávnění." -ForegroundColor Yellow
}

function Ensure-V4 {
    $ok = $false
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:8765/api/health" -TimeoutSec 2
        if ($health.ok) { $ok = $true }
    } catch { }

    if ($ok) {
        Write-Host "V4 server už běží." -ForegroundColor Green
        return
    }

    Write-Host "V4 neběží -> spouštím neutrální sekvenční hunter profil v novém PowerShell okně..." -ForegroundColor Yellow
    $launcher = Join-Path $PSScriptRoot "start_v4_neutral.ps1"
    Start-Process pwsh.exe -ArgumentList "-NoExit","-ExecutionPolicy","Bypass","-File",$launcher

    for ($i=0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:8765/api/health" -TimeoutSec 2
            if ($health.ok) {
                Write-Host "V4 server spuštěn." -ForegroundColor Green
                return
            }
        } catch { }
    }
    throw "V4 server se do 20 sekund nepřihlásil."
}

function Show-V4Status {
    try {
        $s = Invoke-RestMethod "http://127.0.0.1:8765/api/v4/status" -TimeoutSec 5
        Write-Host ""
        Write-Host ("Recorder: " + $s.recorder.running + " | rows=" + $s.recorder.rows + " | last_error=" + $s.recorder.last_error)
        Write-Host ("Alpha:    " + $s.alpha.running + " | paper_equity=" + $s.alpha.paper_equity + " | consensus=" + $s.alpha.consensus_count)
        Write-Host ("Live orders in V4 engine: " + $s.live_orders)
    } catch {
        Write-Host "V4 status endpoint se nepodařilo načíst." -ForegroundColor Yellow
    }
}

Banner "IMPULSE MAX 5K - KOMPLETNÍ KRAKEN SETUP / START" Cyan
Write-Host "Cíl: jedním během dotáhnout závislosti, V4, API údaje a privátní readiness."
Write-Host "Výběry z účtu jsou záměrně zakázané. Readiness používá pouze validate=true, takže neodešle skutečný příkaz."
Write-Host ""

# Keep local checkout current, but never overwrite local edits.
try {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "git pull neprošel; pokračuji s lokální verzí." -ForegroundColor Yellow }
} catch { }

if (-not (Test-Path $Python)) {
    Write-Host "Vytvářím Python virtual environment..."
    py -m venv .venv
}
Set-ExecutionPolicy -Scope Process Bypass -Force
& (Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1")
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Instalace Python dependencies selhala." }

Ensure-V4
Show-V4Status

$source = ""
$cred = Load-ProtectedCredentialStore
if ($cred) {
    Set-Process-Credentials $cred[0] $cred[1]
    $source = "dpapi"
    Write-Host "Použiji lokálně uložené DPAPI údaje." -ForegroundColor Green
}
elseif (Test-Path $LegacyVault) {
    Write-Host "Nejdřív zkusím existující Vault bez vypsání hodnot: $LegacyVault" -ForegroundColor Cyan
    $probe = Invoke-Readiness $LegacyVault
    if ($probe.Report -and $probe.Report.checks.auth_ok) {
        $source = "legacy-env"
        Write-Host "Kraken údaje ve stávajícím Vaultu jsou použitelné." -ForegroundColor Green
    }
    else {
        Write-Host "Stávající Vault nemá strojově použitelný Kraken pár. Přepínám na interaktivní zadání." -ForegroundColor Yellow
    }
}

while ($true) {
    if (-not $source) {
        $cred = Prompt-Credentials
        Set-Process-Credentials $cred[0] $cred[1]
        $source = "interactive"
    }

    if ($source -eq "legacy-env") {
        $result = Invoke-Readiness $LegacyVault
    } else {
        $result = Invoke-Readiness
    }

    if (-not $result.Report) {
        Banner "AUTENTIZACE / API TEST NEPROŠEL" Red
        $safeError = Text $result.ErrorText
        if (-not [string]::IsNullOrWhiteSpace($safeError)) {
            $safeError = $safeError -replace '(?i)(api[-_ ]?key|secret|private[-_ ]?key)\s*[=:]\s*\S+', '$1=<hidden>'
            Write-Host $safeError -ForegroundColor Red
        }

        if ($safeError -match '(?i)otp|2fa|two[- ]factor') {
            Write-Host ""
            Write-Host "Kraken key zřejmě vyžaduje API 2FA/OTP." -ForegroundColor Yellow
            $otp = Read-Host "Zadej aktuální jednorázový API OTP kód"
            if (-not [string]::IsNullOrWhiteSpace((Text $otp))) {
                $env:KRAKEN_OTP = (Text $otp).Trim()
                Write-Host "OTP doplněno pouze pro tento PowerShell. Opakuji test..." -ForegroundColor Cyan
                continue
            }
        }

        Write-Host ""
        Write-Host "Nejčastější příčina: špatný/expirující key, secret, IP whitelist nebo API 2FA." -ForegroundColor Yellow
        $next = Read-Host "ENTER = zadat key+secret znovu; S = otevřít Kraken API settings; Q = skončit"
        if ((ChoiceUpper $next) -eq "Q") {
            Clear-Process-Credentials
            throw "Ukončeno uživatelem."
        }
        if ((ChoiceUpper $next) -eq "S") {
            try { Start-Process "https://pro.kraken.com/app/settings/api" } catch { }
            Read-Host "Po kontrole/vytvoření key stiskni ENTER"
        }
        Clear-Process-Credentials
        $source = ""
        continue
    }

    $r = $result.Report
    if ($r.safe_to_arm -eq $true) {
        Banner "PRIVATE KRAKEN READINESS = OK" Green
        Write-Host "safe_to_arm: true" -ForegroundColor Green
        Write-Host "withdrawals: BLOCKED" -ForegroundColor Green
        Write-Host "validated margin order path: true" -ForegroundColor Green
        Write-Host "private WebSocket token: true" -ForegroundColor Green
        Write-Host "actual_order_submitted: false" -ForegroundColor Green
        Write-Host ("non-zero balances: " + (($r.balance_nonzero.PSObject.Properties.Name) -join ", "))
        Write-Host ("open orders: " + $r.open_orders_count + " | open positions: " + $r.open_positions_count)
        Write-Host ""
        Write-Host "Kapitál / marže dostupná systému:" -ForegroundColor Cyan
        & $Python "capital_sources.py" --report $ReadinessJson
        try {
            & $Python "kraken_inventory.py"
        }
        catch {
            Write-Host "Kraken inventory se nepodařilo vytvořit, readiness report zůstává platný." -ForegroundColor Yellow
        }

        if ($ConsolidateUSDC) {
            Banner "KONSOLIDACE DO USDC" Yellow
            Write-Host "Nejdřív zobrazím plán a validační AddOrder(validate=true)." -ForegroundColor Cyan
            & $Python "consolidate_usdc.py"
            if ($LASTEXITCODE -ne 0) {
                throw "Konsolidační plán/validace selhal."
            }
            Write-Host ""
            Write-Host "Automaticky se provedou pouze přímé Pro prodeje do USDC, které splní Kraken minima." -ForegroundColor Yellow
            Write-Host "BTC/ETH nebo jiné zbytky pod minimem zůstanou k ručnímu Convertu; nic se nebude obcházet." -ForegroundColor Yellow
            $confirmConsolidate = Read-Host "Pro živou konsolidaci napiš přesně SJEDNOTIT USDC"
            if ($confirmConsolidate -eq "SJEDNOTIT USDC") {
                & $Python "consolidate_usdc.py" --execute --confirm SJEDNOTIT-USDC
                if ($LASTEXITCODE -ne 0) {
                    throw "Živá konsolidace Pro-eligible části selhala."
                }
                try {
                    & $Python "kraken_inventory.py"
                } catch {}
            }
            else {
                Write-Host "Živá konsolidace nebyla provedena." -ForegroundColor Yellow
            }
        }
        break
    }

    Show-Permission-Checklist $r
    $choice = Read-Host "Po úpravě Kraken API key stiskni ENTER pro nový test; K = zadat nový key; Q = skončit"
    if ((ChoiceUpper $choice) -eq "Q") {
        Clear-Process-Credentials
        throw "Ukončeno uživatelem před safe_to_arm."
    }
    if ((ChoiceUpper $choice) -eq "K") {
        Clear-Process-Credentials
        $source = ""
    }
}

Show-V4Status

Banner "HOTOVO - SYSTÉM BĚŽÍ A PRIVATE API JE OVĚŘENÉ" Green
Write-Host "Readiness report: $ReadinessJson"
Write-Host "V4 dashboard: http://127.0.0.1:8765"
Write-Host "PAPER/alpha recorder pokračuje. Privátní API cesta je připravena."
Write-Host ""
Write-Host "DŮLEŽITÉ: současný V4 engine stále neposílá LIVE ordery; live_orders=false je záměrný poslední bezpečnostní zámek." -ForegroundColor Yellow
Write-Host "Není potřeba vkládat žádný samostatný token: private WebSocket token získává systém automaticky z API key." -ForegroundColor Cyan
Write-Host "Výběry nejsou povolené ani požadované."
Write-Host ""
try { Start-Process "http://127.0.0.1:8765" } catch { }

Clear-Process-Credentials
