from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any

from futures_private import (
    client_from_env,
    contract_size,
    min_lot,
    order_preflight,
    place_order,
    readiness,
    round_price_to_tick,
    round_size_down,
    save_policy,
)

SYMBOL = "PF_ETHUSD"
RESERVE_PCT = 20.0
DEPLOY_PCT = 80.0
DEFAULT_STOP_PCT = 3.0
DEFAULT_TAKE_PCT = 9.0
MAX_EFFECTIVE_LEVERAGE = 1.0
CONFIRM_TEXT = "SHORT ETH 80"


def _ticker_mid(client: Any, symbol: str) -> float:
    rows = (client.tickers() or {}).get("tickers") or []
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        try:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            if bid > 0 and ask >= bid:
                return (bid + ask) / 2.0
        except Exception:
            pass
        for key in ("markPrice", "last", "indexPrice"):
            try:
                px = float(row.get(key) or 0.0)
                if px > 0:
                    return px
            except Exception:
                continue
    raise RuntimeError(f"No usable ticker for {symbol}")


def build_plan(stop_pct: float, take_pct: float) -> dict[str, Any]:
    r = readiness()
    equity = float(r.get("equity_usd") or 0.0)
    if equity <= 0:
        raise RuntimeError("No usable Futures equity")

    if int(r.get("open_position_count") or 0) > 0:
        raise RuntimeError("Open Futures position exists; this script requires a clean account")

    client = client_from_env()
    px = _ticker_mid(client, SYMBOL)
    csize = contract_size(SYMBOL)
    minimum = min_lot(SYMBOL)

    reserve_usd = equity * RESERVE_PCT / 100.0
    deployable_equity = equity - reserve_usd

    # Borrowed collateral is already debt, so this script deliberately does not
    # add another leverage layer. Target notional = at most 80% of Futures equity.
    target_notional = deployable_equity * MAX_EFFECTIVE_LEVERAGE

    raw_size = target_notional / (px * csize)
    size = round_size_down(SYMBOL, raw_size)
    if size < minimum:
        raise RuntimeError(
            f"80% deployable equity is below minimum lot for {SYMBOL}: "
            f"size={size}, min={minimum}"
        )

    actual_notional = size * px * csize
    reserve_after = equity - actual_notional

    stop = round_price_to_tick(
        SYMBOL, px * (1.0 + stop_pct / 100.0), mode="up"
    )
    take = round_price_to_tick(
        SYMBOL, px * (1.0 - take_pct / 100.0), mode="down"
    )

    # Align generic preflight policy to this explicit one-shot strategy.
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": DEPLOY_PCT,
        "max_order_notional_usd": max(1.0, actual_notional * 1.01),
        "max_open_positions": 1,
        "max_portfolio_notional_usd": max(1.0, actual_notional * 1.01),
        "max_portfolio_notional_pct_equity": DEPLOY_PCT,
        "allowed_roots": ["ETHUSD"],
    })

    pre = order_preflight(
        SYMBOL, "sell", size, reduce_only=False, client=client
    )

    return {
        "symbol": SYMBOL,
        "direction": "SHORT",
        "equity_usd": round(equity, 6),
        "reserve_pct": RESERVE_PCT,
        "reserve_usd": round(reserve_usd, 6),
        "deploy_pct": DEPLOY_PCT,
        "target_notional_usd": round(target_notional, 6),
        "actual_notional_usd": round(actual_notional, 6),
        "approx_reserve_after_notional_usd": round(reserve_after, 6),
        "effective_leverage_cap": MAX_EFFECTIVE_LEVERAGE,
        "mid_price": px,
        "size": size,
        "stop_pct": stop_pct,
        "stop_price": stop,
        "take_pct": take_pct,
        "take_price": take,
        "preflight": pre,
    }


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    client = client_from_env()
    symbol = str(plan["symbol"])
    size = float(plan["size"])

    save_policy({"live_execution": True})
    try:
        entry = place_order(
            symbol,
            "sell",
            size,
            reduce_only=False,
            order_type="mkt",
            cli_ord_id=f"eths{int(time.time() * 1000)}",
            use_deadman=False,
        )
        if not entry.get("submitted_live"):
            raise RuntimeError(f"Entry not accepted: {entry}")

        # Give the exchange a short moment to expose the position.
        time.sleep(0.8)

        stop = place_order(
            symbol,
            "buy",
            size,
            reduce_only=True,
            order_type="stp",
            stop_price=float(plan["stop_price"]),
            limit_price=round_price_to_tick(
                symbol, float(plan["stop_price"]) * 1.002, mode="up"
            ),
            trigger_signal="mark",
            cli_ord_id=f"ethss{int(time.time() * 1000)}",
            use_deadman=False,
        )
        if not stop.get("submitted_live"):
            raise RuntimeError(f"Protective stop not accepted: {stop}")

        take = place_order(
            symbol,
            "buy",
            size,
            reduce_only=True,
            order_type="take_profit",
            stop_price=float(plan["take_price"]),
            limit_price=round_price_to_tick(
                symbol, float(plan["take_price"]) * 1.002, mode="up"
            ),
            trigger_signal="mark",
            cli_ord_id=f"ethst{int(time.time() * 1000)}",
            use_deadman=False,
        )
        if not take.get("submitted_live"):
            raise RuntimeError(f"Take-profit not accepted: {take}")

        return {
            "ok": True,
            "reason": "ETH_LONGTERM_SHORT_OPEN_WITH_PROTECTION",
            "plan": plan,
            "entry": entry,
            "stop": stop,
            "take_profit": take,
        }
    finally:
        save_policy({"live_execution": False})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT)
    ap.add_argument("--take-pct", type=float, default=DEFAULT_TAKE_PCT)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.stop_pct <= 0 or args.take_pct <= 0:
        raise SystemExit("stop/take must be positive")
    if args.stop_pct > 10:
        raise SystemExit("stop-pct above 10% is blocked by this script")

    plan = build_plan(args.stop_pct, args.take_pct)
    print(json.dumps({"plan": plan, "live": args.live}, indent=2, ensure_ascii=False))

    if not args.live:
        return

    if args.confirm != CONFIRM_TEXT:
        raise SystemExit(
            f"LIVE blocked. Re-run with --live --confirm \"{CONFIRM_TEXT}\""
        )

    result = execute(plan)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
