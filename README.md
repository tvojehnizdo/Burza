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
