$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Readiness = Join-Path $PSScriptRoot "reports\kraken-readiness-latest.json"

if (-not (Test-Path $Readiness)) { throw "Kraken readiness report chybi." }
$r = Get-Content -Raw $Readiness | ConvertFrom-Json
if ($r.safe_to_arm -ne $true) { throw "Kraken private API neni safe_to_arm." }

$statsJson = & $Python -c "import json; from live_bridge import paper_stats; print(json.dumps(paper_stats()))"
$stats = $statsJson | ConvertFrom-Json

Write-Host ""
Write-Host "LIVE EVIDENCE GATE" -ForegroundColor Cyan
Write-Host ("closed paper trades: " + $stats.closed)
Write-Host ("paper net P/L CZK:  " + $stats.net_pnl_czk)
Write-Host ("paper win rate:     " + [math]::Round([double]$stats.win_rate*100,2) + "%")
Write-Host ("gate:               " + $stats.gate)

if ($stats.gate -ne $true) {
    Write-Host ""
    Write-Host "LIVE NEZAPNUTO: statisticka evidence gate jeste neprosla." -ForegroundColor Yellow
    exit 2
}

Write-Host ""
Write-Host "Tento krok povoli realne spot/margin ordery z validovanych Pulse signalu." -ForegroundColor Yellow
Write-Host "Withdrawals a wallet transfers zustavaji zakazane." -ForegroundColor Yellow
$confirm = Read-Host "Pro aktivaci napis presne ARM"
if ($confirm -ne "ARM") {
    Write-Host "Neaktivovano."
    exit 1
}

& $Python -c "from kraken_live_control import save_policy; print(save_policy({'live_execution':True}))"
Write-Host ""
Write-Host "LIVE EXECUTION ARMED. AI Supervisor ho muze pouze vypnout, nikdy zapnout." -ForegroundColor Green
