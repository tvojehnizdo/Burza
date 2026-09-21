# IMPULSE MAX 5K — Kraken Pulse Hunter V4

V4 is a **PAPER / research engine**. It does not send live orders.

## What changed

V3 showed that simple candle momentum/breakout/reversion did **not** produce a validated edge on the tested Kraken data. V4 therefore stops optimizing the same indicator stack and records market microstructure directly.

Live inputs:
- Kraken Spot WebSocket v2 L2 order book (book, depth 10)
- Kraken Spot WebSocket v2 matched trades (trade)
- ~1 second local snapshots into SQLite
- BTC cross-market lead signal for ETH/SOL/XRP
- spread, microprice pressure, top-5/top-10 order-book imbalance
- 10s/30s taker-flow imbalance
- short-horizon return and realized volatility
- pressure-without-movement / absorption state

The alpha loop runs every **30 seconds**. It builds 30s and 60s conditional state models with a chronological train/validation split. A PAPER position is allowed only if the **same state and direction survives both horizons after modeled costs**. No model = no trade.

## Run now on Windows

Stop the old Uvicorn window with Ctrl+C, then run:

    cd C:\Burza
    git pull
    .\start_v4.ps1

The launcher installs any missing dependency, starts V4, enables the recorder automatically and opens http://127.0.0.1:8765

Useful endpoints:
- /api/v4/status — recorder, row count, paper equity, model count
- /api/v4/models — validated 30s/60s state models
- /api/v4/paper — PAPER trades
- /api/futures-pulses — cross-asset Kraken derivatives PAPER scan

The local database is data/pulse_v4.db.

## Full-history mode

Kraken publishes complete OHLCVT history through 2026-06-30. The archive is multi-part and large. The full launcher checks for at least 45 GB of free disk, starts the official Kraken archive download in a second PowerShell window, verifies the assembled SHA-256, extracts it, and then runs 5m/15m/60m train-validation-holdout research:

    cd C:\Burza
    git pull
    .\start_v4_full.ps1

Historical output: reports/history-alpha.json.

The live 30-second microstructure edge cannot be reconstructed from 1-minute OHLCVT, so V4 records its own order-book/trade dataset from the moment it starts. Kraken also publishes tick/time-and-sales history; that is the next expansion if the live microstructure states show promise.

## Safety / interpretation

- Start capital model: 5,000 CZK
- PAPER allocation default: 25% of current paper equity
- max model drawdown gate: 10%
- no martingale
- no averaging down
- no live credentials required
- no live orders exist in V4
- modeled fast-execution round trip defaults to 14 bps for the PAPER derivatives research lane and is configurable
- a positive short sample is not accepted as proof; the engine requires repeated train/validation agreement

V4's job is not to manufacture green backtests. Its job is to find repeatable states that remain positive after costs and reject everything else.


## Private Kraken readiness

Run this in a second PowerShell window while V4 keeps collecting market data:

    cd C:\Burza
    git pull
    .\prepare_private.ps1

The readiness script:
- loads the Kraken key/secret locally without printing either value
- calls GetApiKeyInfo
- requires Query Funds, Query Open Orders/Trades and Modify Trades
- fails if Withdraw Funds is enabled
- reads balances, margin/trade balance, open orders and open positions
- sends one AddOrder request with validate=true and leverage=2 to test the margin order path without entering the matching engine
- reports actual_order_submitted=false

This step does not place a live trade. It establishes whether the account/API path is technically ready and whether the no-withdraw boundary is enforced by the API key itself.


## One-click complete setup

If the Vault is not machine-readable or any Kraken credential is missing, use the interactive PowerShell wizard:

    cd C:\Burza
    git pull
    .\spustit_vse.ps1

The wizard:
- updates the repo with fast-forward only
- creates/repairs the Python environment and dependencies
- starts V4 automatically if it is not already running
- tries the existing Vault without exposing values
- if credentials are missing/invalid, asks for API key and secret directly in PowerShell with hidden input
- optionally stores both locally encrypted with Windows DPAPI (current Windows user)
- validates Kraken authentication and reads balances, margin state, open orders and open positions
- requires Query Funds, Query Open Orders & Trades, Create/Modify Orders, Cancel/Close Orders and WebSocket interface
- blocks if Withdraw Funds or withdrawal-address administration is enabled
- validates a 2x margin AddOrder path with validate=true, so no real order enters the matching engine
- opens Kraken API settings and waits for the user to fix permissions if necessary, then retries automatically
- leaves V4 running and writes reports/kraken-readiness-latest.json

This wizard deliberately stops at private API readiness. The current V4 engine still reports live_orders=false; actual live order routing is a separate final activation layer.


## Complete control architecture

Run:

    cd C:\Burza
    git pull
    .\spustit_komplet.ps1

This orchestrates:

1. Spot/margin readiness using the encrypted IMPULSE_V4 key.
2. Optional Kraken Unified Wallet activation in Kraken Pro.
3. Optional separate Futures API key:
   - General API = FULL ACCESS
   - Transfer/Withdrawal API = NO ACCESS
4. Local OpenAI AI Supervisor on http://127.0.0.1:8770.
5. Existing V4 Pulse Engine on http://127.0.0.1:8765.

### Why Unified Wallet
Kraken Unified Wallet combines eligible Spot, Margin and Multi-M Futures collateral in one balance. This is preferred over giving the API key Withdraw Funds just to call legacy Spot<->Futures WalletTransfer. The system therefore does not implement any withdrawal or wallet-transfer endpoint.

### AI Supervisor
The AI supervisor can inspect:
- V4 recorder/alpha status
- paper/consensus models
- Kraken balances, margin, orders and positions
- Futures readiness if a Futures key is configured
- local execution policy

It may directly:
- start/stop V4
- cancel all spot/margin orders
- arm a Futures dead-man switch
- lower allowed leverage or order-size policy
- disable live execution

It may NOT:
- enable live execution
- enable or use withdrawals
- call wallet-transfer endpoints
- increase leverage or risk above the current policy
- invent trades outside the deterministic Pulse Engine

### Execution control
kraken_live_control.py provides the actual Spot/Margin order adapter. It uses Kraken AddOrder and stays validate-only unless the local policy file explicitly has live_execution=true.

futures_private.py provides the Futures private API adapter. It verifies:
- General API permission = FULL_ACCESS
- Transfer permission = NO_ACCESS

The Futures adapter arms cancelallordersafter (dead-man switch) before live Futures orders.

### OpenAI key
The local supervisor needs OPENAI_API_KEY. The launcher asks for it locally and can store it with Windows DPAPI. Never paste API keys into chat.
