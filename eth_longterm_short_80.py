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
    position_map,
    readiness,
    round_price_to_tick,
    round_size_down,
)

SYMBOL = "PF_ETHUSD"
DEFAULT_DEPLOY_PCT = 80.0
DEFAULT_RESERVE_PCT = 20.0
DEFAULT_STOP_PCT = 8.0
DEFAULT_TAKE_PCT = 20.0


def _ticker_mid(client: Any, symbol: str) -> float:
    for row in client.tickers().get("tickers") or []:
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
    raise RuntimeError(f"No usable live ticker for {symbol}")


def _position_size(client: Any, symbol: str) -> float:
    return float(position_map(client.open_positions()).get(symbol.upper(), 0.0))


def build_plan(
    deploy_pct: float,
    stop_pct: float,
    take_pct: float,
) -> dict[str, Any]:
    if not (0 < deploy_pct <= 80.0):
        raise ValueError("deploy_pct must be in (0, 80]")
    if stop_pct <= 0 or take_pct <= 0:
        raise ValueError("stop_pct and take_pct must be positive")

    client = client_from_env()
    r = readiness()
    equity = float(r.get("equity_usd") or 0.0)
    if equity <= 0:
        raise RuntimeError("No Futures equity available")

    existing = _position_size(client, SYMBOL)
    if abs(existing) >= min_lot(SYMBOL):
        raise RuntimeError(f"Existing {SYMBOL} position detected: {existing}")

    px = _ticker_mid(client, SYMBOL)
    csize = contract_size(SYMBOL)

    target_notional = equity * deploy_pct / 100.0
    reserve_usd = equity - target_notional
    raw_size = target_notional / (px * csize)
    size = round_size_down(SYMBOL, raw_size)
    if size < min_lot(SYMBOL):
        raise RuntimeError("Calculated size is below minimum lot")

    actual_notional = size * px * csize
    actual_reserve = equity - actual_notional

    stop_price = round_price_to_tick(
        SYMBOL, px * (1.0 + stop_pct / 100.0), mode="up"
    )
    take_price = round_price_to_tick(
        SYMBOL, px * (1.0 - take_pct / 100.0), mode="down"
    )

    return {
        "symbol": SYMBOL,
        "direction": "SHORT",
        "equity_usd": round(equity, 6),
        "entry_mid": px,
        "deploy_pct_requested": deploy_pct,
        "reserve_pct_target": round(100.0 - deploy_pct, 4),
        "target_notional_usd": round(target_notional, 6),
        "size": size,
        "estimated_notional_usd": round(actual_notional, 6),
        "estimated_cash_reserve_usd": round(actual_reserve, 6),
        "effective_notional_to_equity": round(actual_notional / equity, 6),
        "stop_pct": stop_pct,
        "stop_price": stop_price,
        "take_pct": take_pct,
        "take_price": take_price,
        "note": (
            "Uses at most 80% of CURRENT KRAKEN FUTURES EQUITY. "
            "Spot/Main borrowed funds must already be transferred to Futures manually; "
            "this script has no transfer capability."
        ),
    }


def execute(plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    if confirm != "SHORT-ETH-80":
        raise RuntimeError("Exact confirmation SHORT-ETH-80 required")

    client = client_from_env()
    symbol = str(plan["symbol"])
    size = float(plan["size"])

    if abs(_position_size(client, symbol)) >= min_lot(symbol):
        raise RuntimeError("ETH Futures position appeared after planning; aborting")

    cli_base = int(time.time() * 1000)

    entry = client.send_order(
        symbol=symbol,
        side="sell",
        size=size,
        order_type="mkt",
        reduce_only=False,
        cli_ord_id=f"es{cli_base}",
    )
    status = str((entry.get("sendStatus") or {}).get("status") or "").lower()
    if status not in {"placed", "filled"}:
        raise RuntimeError(f"Entry rejected: {entry}")

    actual = 0.0
    for _ in range(30):
        time.sleep(0.35)
        actual = abs(_position_size(client, symbol))
        if actual >= min_lot(symbol):
            break

    if actual < min_lot(symbol):
        raise RuntimeError("Entry sent but no ETH Futures position became visible")

    protected = round_size_down(symbol, min(actual, size))
    exit_side = "buy"

    stop = client.send_order(
        symbol=symbol,
        side=exit_side,
        size=protected,
        order_type="stp",
        reduce_only=True,
        stop_price=float(plan["stop_price"]),
        limit_price=round_price_to_tick(
            symbol, float(plan["stop_price"]) * 1.003, mode="up"
        ),
        trigger_signal="mark",
        cli_ord_id=f"exs{cli_base}",
    )
    stop_status = str((stop.get("sendStatus") or {}).get("status") or "").lower()
    if stop_status not in {"placed", "filled"}:
        # Protection failed: flatten immediately.
        flat = client.send_order(
            symbol=symbol,
            side="buy",
            size=protected,
            order_type="mkt",
            reduce_only=True,
            cli_ord_id=f"exf{cli_base}",
        )
        raise RuntimeError(f"STOP failed; emergency flatten sent: stop={stop}, flat={flat}")

    take = client.send_order(
        symbol=symbol,
        side=exit_side,
        size=protected,
        order_type="take_profit",
        reduce_only=True,
        stop_price=float(plan["take_price"]),
        limit_price=round_price_to_tick(
            symbol, float(plan["take_price"]) * 1.003, mode="up"
        ),
        trigger_signal="mark",
        cli_ord_id=f"ext{cli_base}",
    )
    take_status = str((take.get("sendStatus") or {}).get("status") or "").lower()
    if take_status not in {"placed", "filled"}:
        # STOP remains live; report incomplete protection and do not silently continue.
        return {
            "ok": False,
            "reason": "ENTRY_AND_STOP_LIVE_TAKE_FAILED",
            "plan": plan,
            "entry": entry,
            "stop": stop,
            "take_profit": take,
            "actual_position_size": actual,
        }

    return {
        "ok": True,
        "reason": "ETH_LONGTERM_SHORT_LIVE_WITH_PROTECTION",
        "plan": plan,
        "entry": entry,
        "stop": stop,
        "take_profit": take,
        "actual_position_size": actual,
    }


def selftest() -> dict[str, Any]:
    checks = {
        "deploy_cap_80": DEFAULT_DEPLOY_PCT == 80.0,
        "reserve_20": DEFAULT_RESERVE_PCT == 20.0,
        "stop_positive": DEFAULT_STOP_PCT > 0,
        "take_positive": DEFAULT_TAKE_PCT > 0,
        "symbol_eth_perp": SYMBOL == "PF_ETHUSD",
    }
    return {"ok": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--deploy-pct", type=float, default=DEFAULT_DEPLOY_PCT)
    ap.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT)
    ap.add_argument("--take-pct", type=float, default=DEFAULT_TAKE_PCT)
    args = ap.parse_args()

    plan = build_plan(args.deploy_pct, args.stop_pct, args.take_pct)
    if args.execute:
        result = execute(plan, args.confirm)
    else:
        result = {"ok": True, "reason": "PLAN_ONLY", "plan": plan}
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
