from __future__ import annotations

import argparse
import json
import math
import os
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from kraken_private import KrakenPrivate
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
RESERVE_PCT = 20.0
DEPLOY_PCT = 80.0
DEFAULT_STOP_PCT = 3.0
DEFAULT_TAKE_PCT = 9.0
MAX_EFFECTIVE_LEVERAGE = 1.0
CONFIRM_TEXT = "TRANSFER AND SHORT ETH 80"
REPORT_PATH = Path("reports/eth-longterm-short-latest.json")
TRANSFER_WAIT_SEC = 35
POSITION_WAIT_SEC = 10


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _spot_client() -> KrakenPrivate:
    key = os.getenv("KRAKEN_API_KEY", "").strip()
    secret = os.getenv("KRAKEN_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("KRAKEN_API_KEY / KRAKEN_API_SECRET not configured")
    return KrakenPrivate(key, secret)


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


def _spot_usdc_available(client: KrakenPrivate) -> dict[str, float]:
    ex = client.private("BalanceEx")
    row = ex.get("USDC") or {}
    if not isinstance(row, dict):
        row = {}
    balance = _num(row.get("balance"), 0.0)
    credit = _num(row.get("credit"), 0.0)
    credit_used = _num(row.get("credit_used"), 0.0)
    hold_trade = _num(row.get("hold_trade"), 0.0)
    available = balance + credit - credit_used - hold_trade
    return {
        "balance": balance,
        "credit": credit,
        "credit_used": credit_used,
        "hold_trade": hold_trade,
        "available": max(0.0, available),
    }


def _spot_key_check(client: KrakenPrivate) -> dict[str, Any]:
    info = client.private("GetApiKeyInfo")
    permissions = {str(x) for x in (info.get("permissions") or [])}
    if "query-funds" not in permissions:
        raise RuntimeError(
            "Spot API key lacks query-funds permission required for balance/wallet transfer"
        )
    return {
        "apiKeyName": info.get("apiKeyName"),
        "permissions": sorted(permissions),
    }


def _futures_clean_state() -> dict[str, Any]:
    r = readiness()
    if not r.get("safe_to_arm"):
        raise RuntimeError(f"Futures account/key not ready: {r}")
    if int(r.get("open_position_count") or 0) != 0:
        raise RuntimeError("Existing Futures position detected; one-shot ETH short requires clean account")
    return r


def _floor_usdc(amount: float) -> float:
    return float(Decimal(str(max(amount, 0.0))).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))


def transfer_plan() -> dict[str, Any]:
    spot = _spot_client()
    spot_key = _spot_key_check(spot)
    spot_usdc = _spot_usdc_available(spot)
    fut = _futures_clean_state()
    fut_equity = _num(fut.get("equity_usd"), 0.0)

    available_usdc = spot_usdc["available"]
    total_pool = available_usdc + fut_equity
    reserve_target = total_pool * RESERVE_PCT / 100.0
    desired_futures_equity = total_pool * DEPLOY_PCT / 100.0

    transfer_needed = max(0.0, desired_futures_equity - fut_equity)
    transfer_cap = max(0.0, available_usdc - reserve_target)
    transfer_amount = _floor_usdc(min(transfer_needed, transfer_cap))

    return {
        "spot_key": spot_key,
        "spot_usdc": spot_usdc,
        "futures_equity_usd": round(fut_equity, 6),
        "total_pool_usd_equivalent": round(total_pool, 6),
        "reserve_pct": RESERVE_PCT,
        "reserve_target_usd": round(reserve_target, 6),
        "deploy_pct": DEPLOY_PCT,
        "desired_futures_equity_usd": round(desired_futures_equity, 6),
        "transfer_amount_usdc": transfer_amount,
        "expected_spot_reserve_usdc": round(max(0.0, available_usdc - transfer_amount), 6),
    }


def _wallet_transfer_usdc(amount: float) -> dict[str, Any]:
    if amount <= 0:
        return {"ok": True, "reason": "NO_TRANSFER_NEEDED", "amount_usdc": 0.0}
    spot = _spot_client()
    result = spot.private(
        "WalletTransfer",
        {
            "asset": "USDC",
            "from": "Spot Wallet",
            "to": "Futures Wallet",
            "amount": f"{amount:.6f}",
        },
    )
    refid = result.get("refid") if isinstance(result, dict) else None
    if not refid:
        raise RuntimeError(f"WalletTransfer returned no refid: {result}")
    return {
        "ok": True,
        "reason": "WALLET_TRANSFER_SUBMITTED",
        "amount_usdc": amount,
        "refid": refid,
    }


def _wait_for_transfer(
    before_spot: float,
    before_futures: float,
    amount: float,
) -> dict[str, Any]:
    if amount <= 0:
        return {
            "ok": True,
            "spot_available_usdc": before_spot,
            "futures_equity_usd": before_futures,
            "reason": "NO_TRANSFER_NEEDED",
        }

    deadline = time.time() + TRANSFER_WAIT_SEC
    last = {}
    while time.time() < deadline:
        try:
            spot_now = _spot_usdc_available(_spot_client())["available"]
            fut_now = _num(readiness().get("equity_usd"), 0.0)
            last = {
                "spot_available_usdc": spot_now,
                "futures_equity_usd": fut_now,
            }
            spot_moved = before_spot - spot_now
            futures_moved = fut_now - before_futures
            # Allow collateral valuation haircuts while requiring a real transfer
            # to be visible on both sides before any order is submitted.
            if spot_moved >= amount * 0.90 and futures_moved >= amount * 0.85:
                return {"ok": True, **last}
        except Exception as exc:
            last = {"poll_error": f"{type(exc).__name__}: {exc}"}
        time.sleep(1.0)

    raise RuntimeError(
        f"Transfer was submitted but not confirmed on both wallets within "
        f"{TRANSFER_WAIT_SEC}s; no trade was sent. Last state: {last}"
    )


def build_post_transfer_plan(stop_pct: float, take_pct: float) -> dict[str, Any]:
    spot = _spot_client()
    spot_usdc = _spot_usdc_available(spot)
    fut = _futures_clean_state()
    fut_equity = _num(fut.get("equity_usd"), 0.0)

    total_pool = spot_usdc["available"] + fut_equity
    reserve_target = total_pool * RESERVE_PCT / 100.0
    if spot_usdc["available"] + 1e-6 < reserve_target:
        raise RuntimeError(
            f"20% external reserve is not preserved: spot available "
            f"{spot_usdc['available']:.6f} < target {reserve_target:.6f}"
        )

    client = client_from_env()
    try:
        client.cancel_all_orders()
    except Exception:
        pass

    px = _ticker_mid(client, SYMBOL)
    csize = contract_size(SYMBOL)
    minimum = min_lot(SYMBOL)

    # Deploy at most 80% of total pool, never more than ~1x Futures equity.
    target_notional = min(
        total_pool * DEPLOY_PCT / 100.0,
        fut_equity * MAX_EFFECTIVE_LEVERAGE,
    )
    raw_size = target_notional / (px * csize)
    size = round_size_down(SYMBOL, raw_size)
    if size < minimum:
        raise RuntimeError(
            f"Deployable capital is below minimum lot: size={size}, min={minimum}"
        )

    actual_notional = size * px * csize
    effective_leverage = actual_notional / fut_equity if fut_equity > 0 else 999.0
    if effective_leverage > MAX_EFFECTIVE_LEVERAGE + 1e-6:
        raise RuntimeError(
            f"Calculated leverage {effective_leverage:.4f} exceeds 1x cap"
        )

    return {
        "symbol": SYMBOL,
        "direction": "SHORT",
        "spot_reserve_usdc": round(spot_usdc["available"], 6),
        "reserve_target_usd": round(reserve_target, 6),
        "futures_equity_usd": round(fut_equity, 6),
        "total_pool_usd_equivalent": round(total_pool, 6),
        "deploy_pct": DEPLOY_PCT,
        "mid_price": px,
        "size": size,
        "contract_size": csize,
        "actual_notional_usd": round(actual_notional, 6),
        "effective_leverage": round(effective_leverage, 6),
        "stop_pct": stop_pct,
        "take_pct": take_pct,
    }


def _send_ok(result: dict[str, Any]) -> bool:
    status = str((result.get("sendStatus") or {}).get("status") or "").lower()
    return status in {"placed", "filled"}


def _current_position(client: Any) -> tuple[float, float | None]:
    payload = client.open_positions()
    signed = _num(position_map(payload).get(SYMBOL), 0.0)
    rows = payload.get("openPositions") or []
    entry = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper() != SYMBOL:
            continue
        for key in ("price", "entryPrice", "avgEntryPrice", "averageEntryPrice"):
            v = _num(row.get(key), 0.0)
            if v > 0:
                entry = v
                break
    return signed, entry


def _cancel_symbol_orders(client: Any) -> None:
    try:
        payload = client.open_orders()
        for row in payload.get("openOrders") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol") or row.get("tradeable") or "").upper() != SYMBOL:
                continue
            oid = str(row.get("order_id") or row.get("orderId") or "").strip()
            cid = str(row.get("cliOrdId") or "").strip()
            try:
                if oid:
                    client.cancel_order(order_id=oid)
                elif cid:
                    client.cancel_order(cli_ord_id=cid)
            except Exception:
                pass
    except Exception:
        pass


def _flatten_if_needed(client: Any) -> dict[str, Any] | None:
    signed, _ = _current_position(client)
    if abs(signed) < min_lot(SYMBOL):
        return None
    side = "sell" if signed > 0 else "buy"
    size = round_size_down(SYMBOL, abs(signed))
    result = client.send_order(
        SYMBOL,
        side,
        size,
        order_type="mkt",
        reduce_only=True,
        cli_ord_id=f"ethxf{int(time.time() * 1000)}",
    )
    return result


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    client = client_from_env()
    size = float(plan["size"])

    # Re-check clean account immediately before the one-shot live entry.
    _futures_clean_state()
    try:
        client.cancel_all_orders()
    except Exception:
        pass

    entry = None
    stop = None
    take = None
    compensation = None
    try:
        entry = client.send_order(
            SYMBOL,
            "sell",
            size,
            order_type="mkt",
            reduce_only=False,
            cli_ord_id=f"eths{int(time.time() * 1000)}",
        )
        if not _send_ok(entry):
            raise RuntimeError(f"Entry rejected: {entry}")

        deadline = time.time() + POSITION_WAIT_SEC
        actual_size = 0.0
        entry_price = None
        while time.time() < deadline:
            signed, px = _current_position(client)
            if signed < 0 and abs(signed) >= min_lot(SYMBOL):
                actual_size = abs(signed)
                entry_price = px
                break
            time.sleep(0.25)
        if actual_size < min_lot(SYMBOL):
            raise RuntimeError("Entry accepted but short position did not become visible")

        protected_size = round_size_down(SYMBOL, actual_size)
        ref_price = entry_price or _ticker_mid(client, SYMBOL)
        stop_price = round_price_to_tick(
            SYMBOL, ref_price * (1.0 + float(plan["stop_pct"]) / 100.0), mode="up"
        )
        take_price = round_price_to_tick(
            SYMBOL, ref_price * (1.0 - float(plan["take_pct"]) / 100.0), mode="down"
        )

        stop = client.send_order(
            SYMBOL,
            "buy",
            protected_size,
            order_type="stp",
            reduce_only=True,
            stop_price=stop_price,
            limit_price=round_price_to_tick(SYMBOL, stop_price * 1.002, mode="up"),
            trigger_signal="mark",
            cli_ord_id=f"ethss{int(time.time() * 1000)}",
        )
        if not _send_ok(stop):
            raise RuntimeError(f"Protective stop rejected: {stop}")

        take = client.send_order(
            SYMBOL,
            "buy",
            protected_size,
            order_type="take_profit",
            reduce_only=True,
            stop_price=take_price,
            limit_price=round_price_to_tick(SYMBOL, take_price * 1.002, mode="up"),
            trigger_signal="mark",
            cli_ord_id=f"ethst{int(time.time() * 1000)}",
        )
        if not _send_ok(take):
            raise RuntimeError(f"Take-profit rejected: {take}")

        result = {
            "ok": True,
            "reason": "ETH_SHORT_OPEN_WITH_20PCT_EXTERNAL_RESERVE",
            "plan": plan,
            "actual_size": protected_size,
            "entry_reference_price": ref_price,
            "stop_price": stop_price,
            "take_price": take_price,
            "entry": entry,
            "stop": stop,
            "take_profit": take,
        }
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return result

    except Exception as exc:
        _cancel_symbol_orders(client)
        try:
            compensation = _flatten_if_needed(client)
        except Exception as comp_exc:
            compensation = {"error": f"{type(comp_exc).__name__}: {comp_exc}"}
        result = {
            "ok": False,
            "reason": "ETH_SHORT_ABORTED_AND_FLATTEN_ATTEMPTED",
            "error": f"{type(exc).__name__}: {exc}",
            "entry": entry,
            "stop": stop,
            "take_profit": take,
            "compensation": compensation,
        }
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        raise RuntimeError(json.dumps(result, ensure_ascii=False, default=str))


def run_all(stop_pct: float, take_pct: float) -> dict[str, Any]:
    before = transfer_plan()
    amount = float(before["transfer_amount_usdc"])

    transfer = _wallet_transfer_usdc(amount)
    transfer_confirmation = _wait_for_transfer(
        float(before["spot_usdc"]["available"]),
        float(before["futures_equity_usd"]),
        amount,
    )

    plan = build_post_transfer_plan(stop_pct, take_pct)
    trade = execute(plan)

    return {
        "ok": True,
        "before": before,
        "transfer": transfer,
        "transfer_confirmation": transfer_confirmation,
        "trade": trade,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT)
    ap.add_argument("--take-pct", type=float, default=DEFAULT_TAKE_PCT)
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.stop_pct <= 0 or args.take_pct <= 0:
        raise SystemExit("stop/take must be positive")
    if args.stop_pct > 10:
        raise SystemExit("stop-pct above 10% is blocked")

    preview = transfer_plan()
    print(json.dumps({"preview": preview}, indent=2, ensure_ascii=False))

    if args.plan or not args.live:
        return

    if args.confirm != CONFIRM_TEXT:
        raise SystemExit(
            f'LIVE blocked. Required: --live --confirm "{CONFIRM_TEXT}"'
        )

    result = run_all(args.stop_pct, args.take_pct)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
