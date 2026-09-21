# IMPULSE MAX 5K — Kraken Pulse Hunter V3

PAPER/REPLAY engine for finding cost-adjusted pulses on Kraken. LIVE orders are disabled.

## Important audit result
Tier-1 Kraken spot fees are high enough that most 30-second/1-minute micro moves should be rejected. The engine therefore uses a hard cost gate instead of forcing activity. Fast-pulse research should be compared against lower-cost Kraken derivatives in PAPER before any LIVE decision.

## Windows
Stop the old server with Ctrl+C, then:

```powershell
cd C:\Burza
git pull
.\.venv\Scripts\Activate.ps1
uvicorn app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765

Endpoints:
- /api/health
- /api/selftest
- /api/pulses
- POST /api/run

The replay uses next-bar entry, conservative stop-first handling when TP and SL are touched in one candle, full modeled round-trip costs, 60/20/20 train-validation-holdout separation, and a hard drawdown guard.

REST OHLC provides only a short recent window. It is a smoke test, not proof of profitability. Durable validation requires Kraken historical OHLCVT/tick data and walk-forward testing over multiple regimes.


## Fast-pulse lane
V2 now also contains a **PAPER-only Kraken derivatives lane** for PF_XBTUSD, PF_ETHUSD and PF_SOLUSD. This is intentionally separate from LIVE. The reason is economic: Tier-1 futures maker fees are materially lower than Tier-1 spot fees, so sub-minute/minute pulses can be tested without forcing a structurally uneconomic spot scalp. The model caps notional below equity and does not enable leverage or live order submission.

Endpoint: /api/futures-pulses


## V3 audit changes
- symmetric LONG/SHORT pulse logic for the PAPER derivatives lane
- next-bar execution remains mandatory to avoid signal-bar lookahead
- hard expected-move / round-trip-cost gate before a candidate can trade
- trend, slow momentum, breakout, range reversion, volume and volatility-shock logic are cross-checked for contradictions
- PAPER derivatives scan widened to BTC, ETH, SOL, gold, silver, WTI oil and selected equity-linked perpetuals
- spot remains long-only and intentionally rejects ordinary micro-scalps when Tier-1 costs dominate the predicted move

This is still a research gate: a profitable holdout is required before any LIVE implementation.
