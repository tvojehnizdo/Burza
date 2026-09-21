$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$BaseUrl = "http://127.0.0.1:8765"
$OutDir = Join-Path $PSScriptRoot "stav_reporty"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$JsonPath = Join-Path $OutDir "impulse_stav_$Stamp.json"
$TxtPath  = Join-Path $OutDir "impulse_stav_$Stamp.txt"

function Safe-InvokeRest {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [int]$TimeoutSec = 8
    )
    try {
        return Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec $TimeoutSec
    }
    catch {
        return [pscustomobject]@{
            __error = $_.Exception.Message
            __uri   = $Uri
        }
    }
}

function Get-Prop {
    param(
        $Object,
        [string]$Name,
        $Default = $null
    )
    if ($null -eq $Object) { return $Default }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $Default }
    return $p.Value
}

function Fmt {
    param($Value, [int]$Digits = 2)
    if ($null -eq $Value) { return "-" }
    try { return ([double]$Value).ToString("N$Digits") }
    catch { return [string]$Value }
}

$gitBranch = "-"
$gitHead = "-"
$gitDirty = "-"
$gitRemote = "-"
try { $gitBranch = (git branch --show-current 2>$null).Trim() } catch {}
try { $gitHead = (git rev-parse --short HEAD 2>$null).Trim() } catch {}
try {
    $dirtyLines = @(git status --porcelain 2>$null)
    $gitDirty = if ($dirtyLines.Count -gt 0) { "ANO" } else { "NE" }
} catch {}
try { $gitRemote = (git remote get-url origin 2>$null).Trim() } catch {}

$engineBuildLocal = "-"
if (Test-Path ".\engine.py") {
    $m = Select-String -Path ".\engine.py" -Pattern 'ENGINE_BUILD\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m -and $m.Matches.Count -gt 0) {
        $engineBuildLocal = $m.Matches[0].Groups[1].Value
    }
}

$listenerInfo = $null
try {
    $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop | Select-Object -First 1
    if ($listener) {
        $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $listenerInfo = [pscustomobject]@{
            listening = $true
            pid       = $listener.OwningProcess
            process   = if ($proc) { $proc.ProcessName } else { $null }
            started   = if ($proc) { $proc.StartTime } else { $null }
        }
    }
}
catch {
    $listenerInfo = [pscustomobject]@{
        listening = $false
        pid       = $null
        process   = $null
        started   = $null
    }
}

$status = Safe-InvokeRest "$BaseUrl/api/v4/status"
$rv     = Safe-InvokeRest "$BaseUrl/api/v4/relative-value"

$statusErr = Get-Prop $status "__error"
$rvErr     = Get-Prop $rv "__error"

$version = Get-Prop $status "version" "-"
$recorder = Get-Prop $status "recorder"
$alpha = Get-Prop $status "alpha"
$rel = Get-Prop $status "relative_value"
$paper = Get-Prop $rv "paper"

if ($null -eq $paper -and $null -ne $rel) {
    $paper = Get-Prop $rel "paper"
}

$warnings = New-Object System.Collections.Generic.List[string]

if ($statusErr) {
    $warnings.Add("STATUS API nedostupné: $statusErr")
}
if ($rvErr) {
    $warnings.Add("RELATIVE VALUE API nedostupné: $rvErr")
}
if ($engineBuildLocal -ne "-" -and $version -ne "-" -and $engineBuildLocal -ne $version) {
    $warnings.Add("Lokální engine.py ($engineBuildLocal) neodpovídá běžícímu serveru ($version).")
}
if ($listenerInfo.listening -ne $true) {
    $warnings.Add("Na portu 8765 nic neposlouchá.")
}

$recorderRunning = Get-Prop $recorder "running" $false
$rvRunning = Get-Prop $rel "running" $false
$rvLastError = Get-Prop $rel "last_error"
$eligible = Get-Prop $rel "eligible_count" 0
$pairsScanned = Get-Prop $rel "pairs_scanned" 0
$historyRows = Get-Prop $rel "history_rows" 0
$liveOrders = Get-Prop $status "live_orders" $false

if (-not $recorderRunning -and -not $statusErr) {
    $warnings.Add("Recorder neběží.")
}
if (-not $rvRunning -and -not $statusErr) {
    $warnings.Add("Relative-value runtime neběží.")
}
if ($rvLastError) {
    $warnings.Add("Relative-value chyba: $rvLastError")
}
if ($liveOrders -eq $true) {
    $warnings.Add("POZOR: live_orders=true.")
}

$openPairs = Get-Prop $paper "open_pairs" 0
$closedPairs = Get-Prop $paper "closed_pairs" 0
$realizedEquity = Get-Prop $paper "realized_equity"
$markedEquity = Get-Prop $paper "marked_equity"

if (($eligible -gt 0) -and ($openPairs -eq 0) -and -not $rvErr) {
    $warnings.Add("Jsou eligible příležitosti, ale PAPER nemá otevřený pár. Zkontrolovat cooldown / executor.")
}
if (($historyRows -lt 60) -and -not $rvErr) {
    $warnings.Add("Historie je ještě krátká (<60 řádků).")
}

$bestRows = @()
$best = Get-Prop $rv "best" @()
if ($best) {
    foreach ($x in @($best) | Select-Object -First 8) {
        $bestRows += [pscustomobject]@{
            root              = Get-Prop $x "root"
            fixed             = Get-Prop $x "fixed_symbol"
            days_to_expiry    = Get-Prop $x "days_to_expiry"
            deviation_bps     = Get-Prop $x "basis_deviation_bps"
            zscore            = Get-Prop $x "zscore"
            friction_bps      = Get-Prop $x "total_friction_bps"
            net_edge_bps      = Get-Prop $x "net_basis_proxy_bps"
            eligible          = Get-Prop $x "eligible" $false
        }
    }
}

$openRows = @()
$open = Get-Prop $paper "open" @()
if ($open) {
    foreach ($x in @($open)) {
        $openRows += [pscustomobject]@{
            id                = Get-Prop $x "id"
            root              = Get-Prop $x "root"
            fixed             = Get-Prop $x "fixed_symbol"
            direction         = Get-Prop $x "direction"
            entry_edge_bps    = Get-Prop $x "entry_edge_bps"
            mark_net_pnl_bps  = Get-Prop $x "mark_net_pnl_bps"
            mark_pnl_czk      = Get-Prop $x "mark_pnl_czk"
        }
    }
}

$report = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    git = [ordered]@{
        branch = $gitBranch
        head = $gitHead
        dirty = $gitDirty
        remote = $gitRemote
    }
    local_engine_build = $engineBuildLocal
    server = $listenerInfo
    api = [ordered]@{
        version = $version
        status_error = $statusErr
        relative_value_error = $rvErr
    }
    recorder = [ordered]@{
        running = $recorderRunning
        rows = Get-Prop $recorder "rows"
        last_message_age_ms = Get-Prop $recorder "last_message_age_ms"
        reconnects = Get-Prop $recorder "reconnects"
        last_error = Get-Prop $recorder "last_error"
    }
    alpha = [ordered]@{
        running = Get-Prop $alpha "running" $false
        paper_equity = Get-Prop $alpha "paper_equity"
        open_trades = Get-Prop $alpha "open_trades"
        closed_trades = Get-Prop $alpha "closed_trades"
        shadow_enabled = Get-Prop (Get-Prop $alpha "shadow") "enabled" $false
        preferred_scenario_enabled = Get-Prop (Get-Prop $alpha "preferred_scenario") "enabled" $false
    }
    relative_value = [ordered]@{
        running = $rvRunning
        last_error = $rvLastError
        scan_interval_s = Get-Prop $rel "scan_interval_s"
        pairs_scanned = $pairsScanned
        eligible_count = $eligible
        history_rows = $historyRows
        best = $bestRows
    }
    paper = [ordered]@{
        enabled = Get-Prop $paper "enabled" $false
        realized_equity = $realizedEquity
        marked_equity = $markedEquity
        pnl_from_start_czk = if ($null -ne $markedEquity) { [math]::Round(([double]$markedEquity - 5000.0), 2) } else { $null }
        open_pairs = $openPairs
        closed_pairs = $closedPairs
        max_open = Get-Prop $paper "max_open"
        alloc_pct_per_leg = Get-Prop $paper "alloc_pct_per_leg"
        take_bps = Get-Prop $paper "take_bps"
        stop_bps = Get-Prop $paper "stop_bps"
        entry_z = Get-Prop $paper "entry_z"
        exit_z = Get-Prop $paper "exit_z"
        stop_z = Get-Prop $paper "stop_z"
        min_history = Get-Prop $paper "min_history"
        max_hold_h = Get-Prop $paper "max_hold_h"
        open = $openRows
    }
    live_orders = $liveOrders
    warnings = @($warnings)
}

$report | ConvertTo-Json -Depth 12 | Set-Content -Path $JsonPath -Encoding UTF8

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("IMPULSE / MPULSE - STAV")
$lines.Add("==============================================")
$lines.Add("Cas:              $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$lines.Add("Git:              $gitBranch @ $gitHead | dirty=$gitDirty")
$lines.Add("Local build:      $engineBuildLocal")
$lines.Add("Server build:     $version")
$lines.Add("Port 8765:        $($listenerInfo.listening) | PID=$($listenerInfo.pid) | $($listenerInfo.process)")
$lines.Add("")
$lines.Add("RECORDER")
$lines.Add("  running:        $recorderRunning")
$lines.Add("  rows:           $(Get-Prop $recorder 'rows')")
$lines.Add("  msg age ms:     $(Get-Prop $recorder 'last_message_age_ms')")
$lines.Add("  reconnects:     $(Get-Prop $recorder 'reconnects')")
$lines.Add("  error:          $(Get-Prop $recorder 'last_error')")
$lines.Add("")
$lines.Add("RELATIVE VALUE V2")
$lines.Add("  running:        $rvRunning")
$lines.Add("  pairs scanned:  $pairsScanned")
$lines.Add("  eligible:       $eligible")
$lines.Add("  history rows:   $historyRows")
$lines.Add("  last error:     $rvLastError")
$lines.Add("")
$lines.Add("PAPER")
$lines.Add("  realized eq:    $(Fmt $realizedEquity) CZK")
$lines.Add("  marked eq:      $(Fmt $markedEquity) CZK")
if ($null -ne $markedEquity) {
    $lines.Add("  PnL od 5000:    $(Fmt ([double]$markedEquity - 5000.0)) CZK")
}
$lines.Add("  open pairs:     $openPairs")
$lines.Add("  closed pairs:   $closedPairs")
$lines.Add("  entry/exit z:   $(Get-Prop $paper 'entry_z') / $(Get-Prop $paper 'exit_z')")
$lines.Add("  stop z:         $(Get-Prop $paper 'stop_z')")
$lines.Add("  take/stop bps:  $(Get-Prop $paper 'take_bps') / $(Get-Prop $paper 'stop_bps')")
$lines.Add("  max hold h:     $(Get-Prop $paper 'max_hold_h')")
$lines.Add("")
$lines.Add("TOP KANDIDATI")
if ($bestRows.Count -eq 0) {
    $lines.Add("  zadna data")
} else {
    foreach ($x in $bestRows) {
        $lines.Add(
            ("  {0,-7} {1,-22} z={2,7} dev={3,9}bps friction={4,9}bps net={5,9}bps eligible={6}" -f
                $x.root,
                $x.fixed,
                (Fmt $x.zscore 3),
                (Fmt $x.deviation_bps 2),
                (Fmt $x.friction_bps 2),
                (Fmt $x.net_edge_bps 2),
                $x.eligible)
        )
    }
}
$lines.Add("")
$lines.Add("OTEVRENE PAPER PARY")
if ($openRows.Count -eq 0) {
    $lines.Add("  zadne")
} else {
    foreach ($x in $openRows) {
        $lines.Add(
            ("  ID={0} {1} {2} {3} | entryEdge={4}bps | markPnL={5}bps / {6} CZK" -f
                $x.id,$x.root,$x.fixed,$x.direction,
                (Fmt $x.entry_edge_bps 2),
                (Fmt $x.mark_net_pnl_bps 2),
                (Fmt $x.mark_pnl_czk 2))
        )
    }
}
$lines.Add("")
$lines.Add("VAROVANI")
if ($warnings.Count -eq 0) {
    $lines.Add("  zadne")
} else {
    foreach ($w in $warnings) { $lines.Add("  - $w") }
}
$lines.Add("")
$lines.Add("live_orders:      $liveOrders")
$lines.Add("JSON:             $JsonPath")
$lines.Add("TXT:              $TxtPath")

$lines | Set-Content -Path $TxtPath -Encoding UTF8
$lines | ForEach-Object { Write-Host $_ }

Write-Host ""
Write-Host "Hotovo. Pro dalsi kontrolu mi staci poslat obsah TXT reportu." -ForegroundColor Green
