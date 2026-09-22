$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Store = "C:\TvojeHnizdo\Vault\Kraken\futures.credentials.dpapi.json"
$PidFile = Join-Path $PSScriptRoot "data\futures_autopilot.pid"

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

Write-Host ""
Write-Host "=== NAPRAVA + KONTROLA + REAL FUTURES ===" -ForegroundColor Cyan
Write-Host "Oprava tick precision | circuit breaker po abortu | prisnejsi vstupy | trailing" -ForegroundColor Yellow
Write-Host ""

if (-not (Test-Path $Python)) { throw "Python venv chybi: $Python" }
if (-not (Test-Path $Store)) { throw "Futures credentials chybi: $Store" }

if (Test-Path $PidFile) {
    try {
        $pidValue = [int](Get-Content -Raw $PidFile).Trim()
        $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($proc) {
            throw "Stara futures relace stale bezi pod PID $pidValue. Nejdrive ji ukonci Ctrl+C."
        }
    } catch {
        if ($_.Exception.Message -like "Stara futures relace*") { throw }
    }
}

$dirty = git status --porcelain
if ($dirty) {
    Write-Host $dirty -ForegroundColor Red
    throw "C:\Burza obsahuje lokalni zmeny. REAL se nespusti, dokud nejsou vyresene."
}

Write-Host "[1/6] Stahuji posledni opravy..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull selhal." }

Write-Host "[2/6] Kompilace..." -ForegroundColor Cyan
& $Python -m py_compile futures_private.py futures_canary.py futures_autopilot.py futures_pairs.py futures_session.py futures_audit.py futures_shadow_learning.py futures_setup_engine.py futures_scale_gate.py futures_setup_research.py futures_microstructure_v4.py
if ($LASTEXITCODE -ne 0) { throw "Python kompilace selhala." }

Write-Host "[3/6] Selftesty..." -ForegroundColor Cyan
& $Python -c "import futures_private, json; r=futures_private.precision_selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Precision selftest selhal." }

& $Python -c "import futures_canary, json; r=futures_canary.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Canary selftest selhal." }

& $Python -c "import futures_shadow_learning, json; r=futures_shadow_learning.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Shadow learning selftest selhal." }

& $Python -c "import futures_setup_engine, json; r=futures_setup_engine.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Setup V3 selftest selhal." }

& $Python -c "import futures_scale_gate, json; r=futures_scale_gate.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Scale gate selftest selhal." }

& $Python -c "import futures_setup_research, json; r=futures_setup_research.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Setup research selftest selhal." }

& $Python -c "import futures_microstructure_v4, json; r=futures_microstructure_v4.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "V4 microstructure selftest selhal." }

& $Python -c "import futures_autopilot, json; r=futures_autopilot.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Autopilot selftest selhal." }

& $Python -c "import futures_pairs, json; r=futures_pairs.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Pairs selftest selhal." }

& $Python -c "import futures_session, json; r=futures_session.selftest(); print(json.dumps(r,indent=2)); assert r['ok']"
if ($LASTEXITCODE -ne 0) { throw "Session selftest selhal." }

Write-Host "[4/6] Read-only kontrola Kraken Futures planu..." -ForegroundColor Cyan
$pair = Load-EncryptedPair $Store
if (-not $pair) { throw "Nelze nacist sifrovane Futures credentials." }

$env:KRAKEN_FUTURES_API_KEY = Secure-ToPlain $pair[0]
$env:KRAKEN_FUTURES_API_SECRET = Secure-ToPlain $pair[1]

try {
    $planText = (& $Python "futures_canary.py" --plan | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Read-only Futures plan selhal." }
    $plan = $planText | ConvertFrom-Json

    if ($plan.ready -and $plan.candidate) {
        $stop = [double]$plan.candidate.stop_price
        $take = [double]$plan.candidate.take_profit_price
        $mid  = [double]$plan.candidate.mid_price
        if ($stop -le 0 -or $take -le 0 -or $mid -le 0) {
            throw "Neplatna ochranna cena v planu: mid=$mid stop=$stop take=$take"
        }
        $msg = "Plan OK: {0} EXEC={1} BASE={2} INVERT={3} | mid={4} stop={5} TP={6}" -f $plan.candidate.symbol,$plan.candidate.execution_signal_side,$plan.candidate.base_signal_side,$plan.candidate.direction_inverted,$mid,$stop,$take
        Write-Host $msg -ForegroundColor Green
    } else {
        Write-Host ("Plan je technicky OK, ale ted neni vhodny signal: {0}" -f $plan.reason) -ForegroundColor Yellow
    }
}
finally {
    Remove-Item Env:KRAKEN_FUTURES_API_KEY,Env:KRAKEN_FUTURES_API_SECRET -ErrorAction SilentlyContinue
}

Write-Host "[5/6] Read-only audit..." -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File ".\audit_futures.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Audit skoncil chybou; REAL se z bezpecnostnich duvodu nespusti." -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "NASTAVENI PRO TUTO VERZI:" -ForegroundColor Yellow
Write-Host " - siroky PF trh: top 48 levny prefilter / 28 deep analyza"
Write-Host " - max spread 20 bps"
Write-Host " - minimalni modelovany net edge 25 bps"
Write-Host " - volume ratio minimalne 0.60 pro samostatny signal"
Write-Host " - Setup V4: uzavrene 1m svicky + orderbook + BTC/ETH leader context + fresh taker flow"
Write-Host " - 3min breakout je pouze SHADOW PROBE, bez realne objednavky"
Write-Host " - LIVE vstup az po potvrzenem 15min range breakoutu"
Write-Host " - breakout buffer 0.05 %, minimalni pohyb od anchoru 0.35 %"
Write-Host " - po uzavreni prvniho obchodu je povolen max 1 potvrzeny reversal pres opacnou hranu ulozeneho range"
Write-Host " - max 2 skutecne vstupy za session: FIRST + REVERSAL"
Write-Host " - breakout s objemem nebo neprestreleny trend-continuation vstup"
Write-Host " - volatility-managed size cca 2-5 USD podle ATR"
Write-Host " - market breadth nesmi byt proti; BTC+ETH nesmi souhlasne tlacit opacne"
Write-Host " - Futures L2 orderbook: extremni opacny tlak = veto; souhlasny tlak zvysuje ranking"
Write-Host " - maker-first vstup: post-only na touch; ELITE ceka max ~0.45 s, ostatni max ~1.5 s; pak bezpecny taker fallback"
Write-Host " - jeden aktivni setup; zadne sekani trhu mnoha nezavislymi vstupy"
Write-Host " - puvodni quality/microstructure filtry zustavaji jako potvrzeni setupu"
Write-Host " - TWO_SIDED trh dovoluje LONG i SHORT bez vynuceneho parovani"
Write-Host " - jednoduche korelacni pary pouze SHADOW; robustni RV oddelene"
Write-Host " - base target 5 USD; vyssi notional se odemkne jen po auditovanem profit evidence; cap 10 USD / 35 % Futures equity"
Write-Host " - STOP cca 45 bps; failed breakout se zavira po potvrzenem navratu do range"
Write-Host " - trailing od +45 bps; aktivni winner uz neni useknut generic 8min timeoutem"
Write-Host " - ratchet gap 18 -> 14 -> 10 -> 8 -> 6 bps"
Write-Host " - backup TP +300 bps"
Write-Host " - shadow-learning dal meri neprovedene kandidaty na 1/3/5/10 min"
Write-Host " - 2 net ztraty po sobe = 10 min pauza; 3 = stop novych vstupu"
Write-Host " - soft session brzda pri cca 1.5 % poklesu equity; hard kill-switch 5 %"
Write-Host " - po skutecne odeslanem abortu se dalsi vstupy ZASTAVI"
Write-Host ""
Write-Host "Pozor: zadne nastaveni nezarucuje zisk." -ForegroundColor Yellow
Write-Host ""

$confirm = Read-Host "Pro REAL relaci napis presne: SPUSTIT REAL"
if ($confirm -ne "SPUSTIT REAL") {
    Write-Host "REAL nebyl spusten." -ForegroundColor Yellow
    exit 0
}

Write-Host "[6/6] SPOUSTIM REAL..." -ForegroundColor Green
powershell -ExecutionPolicy Bypass -File ".\spustit_futures_session.ps1"
$sessionExit = $LASTEXITCODE

Write-Host ""
Write-Host "Po-session audit..." -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File ".\audit_futures.ps1"

if ($sessionExit -ne 0) {
    throw "REAL session skoncila s chybou."
}

Write-Host "REAL session dokoncena." -ForegroundColor Green
