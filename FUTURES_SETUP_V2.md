# Futures Setup V2 — implementation specification

## Goal

Replace frequent momentum/inverse re-entry with a stateful, selective crypto setup inspired by the useful structural ideas in the attached 0DTE paper system.

The crypto implementation keeps linear-futures risk controls. It does **not** copy option delta, option premium stop, or 4x option payoff assumptions.

## Mapping from the source system

- Early probe after 3 minutes -> **3-minute SHADOW probe only** in V2. No live order yet.
- 15-minute opening-range breakout -> **15-minute confirmed rolling-range breakout** on Kraken 1m completed candles.
- Breakout buffer -> initial hypothesis **0.05%**.
- Minimum move from setup anchor -> initial hypothesis **0.35%**.
- First direction -> one confirmed LIVE trade.
- Reversal -> allowed only after the first LIVE trade is fully closed and price breaks the opposite side of the stored 15m range.
- One reversal maximum -> preserved.
- One independent setup at a time -> preserved.
- Add only to winner -> not enabled in first implementation. Revisit after data.
- Long-option convex payoff -> not transferable to perpetual futures and therefore not copied.

## State machine

1. IDLE
2. PROBE_SHADOW
   - candidate passed existing liquidity/trend/volume/flow filters;
   - 3m rolling breakout aligns with candidate direction;
   - no real order.
3. CONFIRMED_READY
   - same symbol/direction breaks 15m range by buffer;
   - move from range anchor >= configured minimum.
4. FIRST_LIVE
   - exactly one protected futures position.
5. WAIT_REVERSAL
   - entered only after FIRST_LIVE is closed.
6. REVERSAL_LIVE
   - price crossed the opposite side of the fixed original 15m range;
   - current Kraken qualification also confirms the reversal direction;
   - one reversal only.
7. DONE
   - no more entries for that setup/session.

## Risk invariants

- No permanent inverse mode.
- No simultaneous long+short hedge on the same setup.
- No averaging down.
- No new independent setup while a live setup exists.
- Existing exchange STOP/TP, trailing, technical-abort circuit breaker, session loss brakes and max-notional limits stay active.
- Maximum actual entries for V2 session: 2 (first leg + one reversal).
- Early probe is SHADOW until forward data justify making it live.

## Initial parameters

- probe lookback: 3 completed 1m bars
- confirmed range: 15 completed 1m bars
- breakout buffer: 0.0005
- minimum move from anchor: 0.0035
- shadow probe expiry: 20 minutes
- LIVE sizing: existing volatility-managed 2–5 USD
- current hard stop/trailing system: unchanged initially

These are hypotheses transferred from the source structure, not claimed crypto-optimal values. Shadow learning must compare them before optimization.

## Acceptance tests before LIVE

- synthetic LONG breakout test
- synthetic SHORT breakout test
- fixed-range reversal test
- only completed candles may trigger
- first LIVE entry cannot occur on a probe alone
- reversal cannot occur before first leg closes
- reversal cannot occur twice
- session actual-entry cap = 2
- existing precision/protection/circuit-breaker tests remain green

## What to optimize later from our own data

- 3m probe usefulness
- 15m vs 10m/20m confirmation
- 5 bps breakout buffer
- 35 bps minimum move
- whether live probe adds positive expectancy
- whether reversal has positive expectancy
- maker-first entry after signal quality is established
