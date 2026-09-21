from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BASE = "https://futures.kraken.com/derivatives/api/v3"
DB_PATH = Path(os.getenv("RV_DB", "data/relative_value.db"))
SCAN_INTERVAL_S = float(os.getenv("RV_SCAN_INTERVAL_S", "15"))
MAKER_FEE_BPS = float(os.getenv("RV_MAKER_FEE_BPS", os.getenv("KRAKEN_FUTURES_MAKER_BPS", "2")))
ADVERSE_BUFFER_BPS = float(os.getenv("RV_ADVERSE_BUFFER_BPS", "4"))
MIN_NET_EDGE_BPS = float(os.getenv("RV_MIN_NET_EDGE_BPS", "8"))
MIN_DAYS_TO_EXPIRY = float(os.getenv("RV_MIN_DAYS_TO_EXPIRY", "0.5"))
MAX_DAYS_TO_EXPIRY = float(os.getenv("RV_MAX_DAYS_TO_EXPIRY", "220"))
HISTORY_WINDOW = int(os.getenv("RV_HISTORY_WINDOW", "500"))
MIN_HISTORY = int(os.getenv("RV_MIN_HISTORY", "60"))
ENTRY_Z = float(os.getenv("RV_ENTRY_Z", "2.0"))
EXIT_Z = float(os.getenv("RV_EXIT_Z", "0.5"))
STOP_Z = float(os.getenv("RV_STOP_Z", "3.5"))

PAPER_ENABLED = os.getenv("RV_PAPER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
PAPER_START_EQUITY = float(os.getenv("RV_PAPER_START_EQUITY", os.getenv("START_CAPITAL", "5000")))
PAPER_ALLOC_PCT = float(os.getenv("RV_PAPER_ALLOC_PCT", "15")) / 100.0
PAPER_MAX_OPEN = int(os.getenv("RV_PAPER_MAX_OPEN", "2"))
PAPER_TAKE_BPS = float(os.getenv("RV_PAPER_TAKE_BPS", "6"))
PAPER_STOP_BPS = float(os.getenv("RV_PAPER_STOP_BPS", "60"))
PAPER_MAX_HOLD_H = float(os.getenv("RV_PAPER_MAX_HOLD_H", "6"))
PAPER_REENTRY_COOLDOWN_S = float(os.getenv("RV_PAPER_REENTRY_COOLDOWN_S", "300"))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ImpulseMax5K-RelativeValue/1.0", "Accept": "application/json"})

FF_RE = re.compile(r"^FF_([A-Z0-9]+USD)_(\d{6})$")


def now_ms() -> int:
    return int(time.time() * 1000)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _mid(t: dict[str, Any]) -> float | None:
    bid, ask = _f(t.get("bid")), _f(t.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    for k in ("markPrice", "last", "indexPrice"):
        v = _f(t.get(k))
        if v is not None and v > 0:
            return v
    return None


def _expiry_from_symbol(symbol: str) -> datetime | None:
    m = FF_RE.match(symbol)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _root_from_fixed(symbol: str) -> str | None:
    m = FF_RE.match(symbol)
    return m.group(1) if m else None


def _public(path: str) -> dict[str, Any]:
    r = SESSION.get(BASE + path, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("result") == "error":
        raise RuntimeError(str(body.get("error") or body.get("errors") or body))
    return body


def market_snapshot() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    instruments_body = _public("/instruments")
    tickers_body = _public("/tickers")
    instruments = instruments_body.get("instruments") or []
    tickers = tickers_body.get("tickers") or []
    ticker_map = {
        str(t.get("symbol")): t
        for t in tickers
        if isinstance(t, dict) and t.get("symbol")
    }
    return instruments, ticker_map


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rv_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                root TEXT NOT NULL,
                perp_symbol TEXT NOT NULL,
                fixed_symbol TEXT NOT NULL,
                expiry TEXT,
                days_to_expiry REAL,
                direction TEXT NOT NULL,
                perp_bid REAL,
                perp_ask REAL,
                fixed_bid REAL,
                fixed_ask REAL,
                mid_basis_bps REAL,
                executable_basis_bps REAL,
                fee_floor_bps REAL,
                adverse_buffer_bps REAL,
                net_basis_proxy_bps REAL,
                funding_rate_raw REAL,
                funding_bps_per_hour REAL,
                zscore REAL,
                eligible INTEGER NOT NULL
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_rv_pair_ts ON rv_snapshots(perp_symbol,fixed_symbol,ts_ms)"
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS rv_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS rv_paper_pairs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                root TEXT NOT NULL,
                perp_symbol TEXT NOT NULL,
                fixed_symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                notional_per_leg_czk REAL NOT NULL,
                entry_perp_px REAL NOT NULL,
                entry_fixed_px REAL NOT NULL,
                entry_edge_bps REAL NOT NULL,
                entry_funding_bps_h REAL,
                neutral_units REAL,
                exit_perp_px REAL,
                exit_fixed_px REAL,
                gross_pnl_czk REAL,
                fees_czk REAL,
                funding_proxy_czk REAL,
                pnl_czk REAL,
                net_pnl_bps REAL,
                exit_reason TEXT,
                status TEXT NOT NULL
            )"""
        )
        cols = {str(r[1]) for r in con.execute("PRAGMA table_info(rv_paper_pairs)").fetchall()}
        if "neutral_units" not in cols:
            con.execute("ALTER TABLE rv_paper_pairs ADD COLUMN neutral_units REAL")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_rv_paper_status ON rv_paper_pairs(status,opened_ms)"
        )


def _meta_get(key: str, default: float, path: Path = DB_PATH) -> float:
    init_db(path)
    with sqlite3.connect(path) as con:
        row = con.execute("SELECT value FROM rv_meta WHERE key=?", (key,)).fetchone()
    if not row:
        return float(default)
    try:
        return float(row[0])
    except Exception:
        return float(default)


def _meta_set(key: str, value: float, path: Path = DB_PATH) -> None:
    init_db(path)
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT INTO rv_meta(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(float(value))),
        )


def paper_equity(path: Path = DB_PATH) -> float:
    return _meta_get("paper_equity", PAPER_START_EQUITY, path)


def _history_stats(perp: str, fixed: str, current_mid_basis: float, path: Path = DB_PATH) -> dict[str, Any]:
    init_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT mid_basis_bps
               FROM rv_snapshots
               WHERE perp_symbol=? AND fixed_symbol=?
               ORDER BY id DESC LIMIT ?""",
            (perp, fixed, HISTORY_WINDOW),
        ).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    n = len(vals)
    if n < 2:
        return {"n": n, "mean": None, "sd": None, "z": None, "deviation_bps": None}
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / max(n - 1, 1)
    sd = math.sqrt(var)
    deviation = current_mid_basis - mean
    z = deviation / sd if sd > 1e-9 else 0.0
    return {"n": n, "mean": mean, "sd": sd, "z": z, "deviation_bps": deviation}


def _funding_bps_per_hour(ticker: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = _f(ticker.get("fundingRate"))
    if raw is None:
        raw = _f(ticker.get("fundingRatePrediction"))
    if raw is None:
        return None, None
    # Kraken ticker fundingRate is the absolute hourly funding amount per
    # contract unit. Convert it to a relative rate using the underlying/index
    # price so it can be compared with basis in bps.
    ref = _f(ticker.get("indexPrice")) or _mid(ticker)
    if ref is None or ref <= 0:
        return raw, None
    return raw, (raw / ref) * 10000.0


def scan_opportunities(
    instruments: list[dict[str, Any]] | None = None,
    tickers: dict[str, dict[str, Any]] | None = None,
    db_path: Path = DB_PATH,
    persist: bool = True,
) -> dict[str, Any]:
    if instruments is None or tickers is None:
        instruments, tickers = market_snapshot()

    active_symbols: set[str] = set()
    for x in instruments:
        if not isinstance(x, dict) or not x.get("symbol"):
            continue
        tradeable = x.get("tradeable")
        if tradeable is False:
            continue
        active_symbols.add(str(x["symbol"]))

    # Some public payloads omit / lag instrument metadata. Tickers are enough
    # for research scanning, while execution would require explicit validation.
    if not active_symbols:
        active_symbols = set(tickers)

    fixed_symbols = sorted(
        s for s in active_symbols
        if s.startswith("FF_") and _root_from_fixed(s) and s in tickers
    )

    generated = now_ms()
    now_dt = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    fee_floor = 4.0 * MAKER_FEE_BPS  # two legs, entry + exit, normalized to one-leg notional

    for fixed_symbol in fixed_symbols:
        root = _root_from_fixed(fixed_symbol)
        if not root:
            continue
        perp_symbol = "PF_" + root
        if perp_symbol not in tickers:
            continue
        if active_symbols and perp_symbol not in active_symbols:
            continue

        expiry = _expiry_from_symbol(fixed_symbol)
        if expiry is None:
            continue
        days = (expiry - now_dt).total_seconds() / 86400.0
        if days < MIN_DAYS_TO_EXPIRY or days > MAX_DAYS_TO_EXPIRY:
            continue

        pt, ft = tickers[perp_symbol], tickers[fixed_symbol]
        pb, pa = _f(pt.get("bid")), _f(pt.get("ask"))
        fb, fa = _f(ft.get("bid")), _f(ft.get("ask"))
        pm, fm = _mid(pt), _mid(ft)
        if None in (pb, pa, fb, fa, pm, fm):
            continue
        assert pb is not None and pa is not None and fb is not None and fa is not None
        assert pm is not None and fm is not None
        if min(pb, pa, fb, fa, pm, fm) <= 0:
            continue

        # One scalar basis series (fixed vs perpetual) is used for statistical
        # relative-value discovery. We trade deviations from its own history,
        # not the absolute carry to expiry.
        mid_basis = (fm / pm - 1.0) * 10000.0
        stats = _history_stats(perp_symbol, fixed_symbol, mid_basis, db_path)
        z = stats["z"]
        n_hist = int(stats["n"])
        mean_basis = stats["mean"]
        deviation_bps = stats["deviation_bps"]

        if deviation_bps is not None and deviation_bps >= 0:
            direction = "LONG_PERP_SHORT_FIXED"
            executable = (fb / pa - 1.0) * 10000.0
        else:
            direction = "SHORT_PERP_LONG_FIXED"
            executable = (pb / fa - 1.0) * 10000.0

        perp_spread_bps = ((pa - pb) / pm) * 10000.0
        fixed_spread_bps = ((fa - fb) / fm) * 10000.0
        spread_roundtrip_bps = max(perp_spread_bps, 0.0) + max(fixed_spread_bps, 0.0)

        funding_raw, funding_bph = _funding_bps_per_hour(pt)
        funding_effect = None
        if funding_bph is not None:
            # Positive relative funding means perp longs pay shorts.
            funding_effect = -funding_bph if direction.startswith("LONG_PERP") else funding_bph

        adverse_funding_bps = 0.0
        if funding_effect is not None and funding_effect < 0:
            adverse_funding_bps = abs(funding_effect) * PAPER_MAX_HOLD_H

        deviation_abs = abs(float(deviation_bps or 0.0))
        total_friction_bps = fee_floor + ADVERSE_BUFFER_BPS + spread_roundtrip_bps + adverse_funding_bps
        net_proxy = deviation_abs - total_friction_bps
        annualized_basis_pct = (mid_basis / 100.0) * (365.0 / days) if days > 0 else None
        eligible = bool(
            n_hist >= MIN_HISTORY
            and z is not None
            and abs(float(z)) >= ENTRY_Z
            and net_proxy >= MIN_NET_EDGE_BPS
        )

        item = {
            "root": root,
            "perp_symbol": perp_symbol,
            "fixed_symbol": fixed_symbol,
            "expiry": expiry.isoformat(),
            "days_to_expiry": round(days, 4),
            "direction": direction,
            "perp_bid": pb,
            "perp_ask": pa,
            "fixed_bid": fb,
            "fixed_ask": fa,
            "mid_basis_bps": round(mid_basis, 4),
            "historical_mean_basis_bps": round(float(mean_basis), 4) if mean_basis is not None else None,
            "basis_deviation_bps": round(float(deviation_bps), 4) if deviation_bps is not None else None,
            "executable_basis_bps": round(executable, 4),
            "annualized_mid_basis_pct": round(annualized_basis_pct, 3) if annualized_basis_pct is not None else None,
            "maker_fee_floor_bps": round(fee_floor, 4),
            "spread_roundtrip_bps": round(spread_roundtrip_bps, 4),
            "adverse_buffer_bps": round(ADVERSE_BUFFER_BPS, 4),
            "adverse_funding_bps_max_hold": round(adverse_funding_bps, 4),
            "total_friction_bps": round(total_friction_bps, 4),
            "net_basis_proxy_bps": round(net_proxy, 4),
            "funding_rate_raw": funding_raw,
            "funding_bps_per_hour": round(funding_bph, 6) if funding_bph is not None else None,
            "funding_effect_bps_1h_for_direction": round(funding_effect, 6) if funding_effect is not None else None,
            "history_n": n_hist,
            "min_history": MIN_HISTORY,
            "entry_z_threshold": ENTRY_Z,
            "zscore": round(float(z), 3) if z is not None else None,
            "eligible": eligible,
            "research_only": True,
            "live_orders": False,
        }
        rows.append(item)

    rows.sort(
        key=lambda x: (
            bool(x["eligible"]),
            float(x["net_basis_proxy_bps"]),
            abs(float(x.get("zscore") or 0.0)),
        ),
        reverse=True,
    )

    if persist and rows:
        init_db(db_path)
        with sqlite3.connect(db_path) as con:
            con.executemany(
                """INSERT INTO rv_snapshots(
                    ts_ms,root,perp_symbol,fixed_symbol,expiry,days_to_expiry,direction,
                    perp_bid,perp_ask,fixed_bid,fixed_ask,mid_basis_bps,executable_basis_bps,
                    fee_floor_bps,adverse_buffer_bps,net_basis_proxy_bps,
                    funding_rate_raw,funding_bps_per_hour,zscore,eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        generated, x["root"], x["perp_symbol"], x["fixed_symbol"], x["expiry"],
                        x["days_to_expiry"], x["direction"], x["perp_bid"], x["perp_ask"],
                        x["fixed_bid"], x["fixed_ask"], x["mid_basis_bps"],
                        x["executable_basis_bps"], x["maker_fee_floor_bps"],
                        x["adverse_buffer_bps"], x["net_basis_proxy_bps"],
                        x["funding_rate_raw"], x["funding_bps_per_hour"], x["zscore"],
                        1 if x["eligible"] else 0,
                    )
                    for x in rows
                ],
            )

    return {
        "generated_ms": generated,
        "mode": "MARKET_NEUTRAL_RELATIVE_VALUE_RESEARCH",
        "pairs_scanned": len(rows),
        "eligible_count": sum(1 for x in rows if x["eligible"]),
        "maker_fee_bps_one_execution": MAKER_FEE_BPS,
        "pair_roundtrip_fee_floor_bps": fee_floor,
        "adverse_buffer_bps": ADVERSE_BUFFER_BPS,
        "min_net_edge_bps": MIN_NET_EDGE_BPS,
        "min_history": MIN_HISTORY,
        "entry_z": ENTRY_Z,
        "exit_z": EXIT_Z,
        "stop_z": STOP_Z,
        "best": rows[:10],
        "eligible": [x for x in rows if x["eligible"]][:20],
        "all": rows,
        "note": (
            "Basis/funding scanner only. It does not guarantee convergence or fills; "
            "perpetual funding, leg risk, liquidity and margin remain material risks."
        ),
        "live_orders": False,
    }


def _pair_map(scan: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(x.get("perp_symbol")), str(x.get("fixed_symbol"))): x
        for x in scan.get("all", [])
        if x.get("perp_symbol") and x.get("fixed_symbol")
    }


def _paper_mark(trade: tuple[Any, ...], quote: dict[str, Any], now: int) -> dict[str, float | int]:
    (
        trade_id, opened_ms, direction, notional, entry_perp, entry_fixed,
        entry_funding_bps_h, neutral_units,
    ) = trade
    n = float(notional)
    u = float(neutral_units or (n / max(float(entry_perp), float(entry_fixed))))
    hours = max(0.0, (now - int(opened_ms)) / 3_600_000.0)
    fee_rate = MAKER_FEE_BPS / 10000.0

    pb = float(quote["perp_bid"])
    pa = float(quote["perp_ask"])
    fb = float(quote["fixed_bid"])
    fa = float(quote["fixed_ask"])

    if direction == "LONG_PERP_SHORT_FIXED":
        exit_perp = pb
        exit_fixed = fa
        gross = u * ((exit_perp - float(entry_perp)) + (float(entry_fixed) - exit_fixed))
        funding_sign = -1.0
    else:
        exit_perp = pa
        exit_fixed = fb
        gross = u * ((float(entry_perp) - exit_perp) + (exit_fixed - float(entry_fixed)))
        funding_sign = 1.0

    # Equal normalized base exposure on both legs: fee/funding scale off actual
    # leg notionals instead of assuming identical percentage returns.
    fees = fee_rate * u * (
        float(entry_perp) + float(entry_fixed) + float(exit_perp) + float(exit_fixed)
    )
    funding_bps_h = float(entry_funding_bps_h or 0.0)
    perp_entry_notional = u * float(entry_perp)
    funding_proxy = perp_entry_notional * funding_sign * funding_bps_h * hours / 10000.0
    pnl = gross - fees + funding_proxy
    reference_notional = u * ((float(entry_perp) + float(entry_fixed)) / 2.0)
    net_bps = pnl / reference_notional * 10000.0 if reference_notional > 0 else 0.0

    return {
        "id": int(trade_id),
        "exit_perp_px": exit_perp,
        "exit_fixed_px": exit_fixed,
        "gross_pnl_czk": gross,
        "fees_czk": fees,
        "funding_proxy_czk": funding_proxy,
        "pnl_czk": pnl,
        "net_pnl_bps": net_bps,
        "hours": hours,
    }


def resolve_paper_pairs(scan: dict[str, Any], path: Path = DB_PATH) -> list[dict[str, Any]]:
    if not PAPER_ENABLED:
        return []
    qmap = _pair_map(scan)
    now = now_ms()
    closed: list[dict[str, Any]] = []
    init_db(path)

    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT id,opened_ms,direction,notional_per_leg_czk,
                      entry_perp_px,entry_fixed_px,entry_funding_bps_h,neutral_units,
                      perp_symbol,fixed_symbol
               FROM rv_paper_pairs WHERE status='OPEN'"""
        ).fetchall()

        for r in rows:
            quote = qmap.get((str(r[8]), str(r[9])))
            if not quote:
                continue
            mark = _paper_mark(r[:8], quote, now)
            reason = None
            current_z = quote.get("zscore")
            net_pnl_bps = float(mark["net_pnl_bps"])
            if net_pnl_bps >= PAPER_TAKE_BPS:
                reason = "TAKE_PNL"
            elif (
                current_z is not None
                and abs(float(current_z)) <= EXIT_Z
                and net_pnl_bps > 0
            ):
                reason = "TAKE_MEAN_REVERSION"
            elif (
                current_z is not None
                and abs(float(current_z)) >= STOP_Z
                and net_pnl_bps < 0
            ):
                reason = "STOP_Z_DIVERGENCE"
            elif net_pnl_bps <= -PAPER_STOP_BPS:
                reason = "STOP_PNL"
            elif float(mark["hours"]) >= PAPER_MAX_HOLD_H:
                reason = "MAX_HOLD"
            if reason is None:
                continue

            con.execute(
                """UPDATE rv_paper_pairs
                   SET closed_ms=?,exit_perp_px=?,exit_fixed_px=?,gross_pnl_czk=?,
                       fees_czk=?,funding_proxy_czk=?,pnl_czk=?,net_pnl_bps=?,
                       exit_reason=?,status='CLOSED'
                   WHERE id=?""",
                (
                    now, mark["exit_perp_px"], mark["exit_fixed_px"], mark["gross_pnl_czk"],
                    mark["fees_czk"], mark["funding_proxy_czk"], mark["pnl_czk"],
                    mark["net_pnl_bps"], reason, mark["id"],
                ),
            )
            closed.append({
                "id": int(mark["id"]),
                "reason": reason,
                "pnl_czk": round(float(mark["pnl_czk"]), 4),
                "net_pnl_bps": round(float(mark["net_pnl_bps"]), 4),
            })

    if closed:
        _meta_set(
            "paper_equity",
            paper_equity(path) + sum(float(x["pnl_czk"]) for x in closed),
            path,
        )
    return closed


def maybe_open_paper_pairs(scan: dict[str, Any], path: Path = DB_PATH) -> list[dict[str, Any]]:
    if not PAPER_ENABLED:
        return []

    candidates = [x for x in scan.get("eligible", []) if bool(x.get("eligible"))]
    if not candidates:
        return []

    now = now_ms()
    init_db(path)
    with sqlite3.connect(path) as con:
        open_rows = con.execute(
            "SELECT perp_symbol,fixed_symbol FROM rv_paper_pairs WHERE status='OPEN'"
        ).fetchall()
        recent_rows = con.execute(
            """SELECT perp_symbol,fixed_symbol,MAX(COALESCE(closed_ms,opened_ms))
               FROM rv_paper_pairs GROUP BY perp_symbol,fixed_symbol"""
        ).fetchall()

    open_keys = {(str(r[0]), str(r[1])) for r in open_rows}
    recent_ms = {(str(r[0]), str(r[1])): int(r[2]) for r in recent_rows if r[2] is not None}
    slots = max(0, PAPER_MAX_OPEN - len(open_keys))
    if slots <= 0:
        return []

    equity = paper_equity(path)
    notional = max(0.0, min(equity * PAPER_ALLOC_PCT, equity))
    if notional <= 0:
        return []

    opened: list[dict[str, Any]] = []
    for x in candidates:
        if len(opened) >= slots:
            break
        key = (str(x["perp_symbol"]), str(x["fixed_symbol"]))
        if key in open_keys:
            continue
        prev = recent_ms.get(key)
        if prev is not None and now - prev < int(PAPER_REENTRY_COOLDOWN_S * 1000):
            continue

        direction = str(x["direction"])
        if direction == "LONG_PERP_SHORT_FIXED":
            entry_perp = float(x["perp_ask"])
            entry_fixed = float(x["fixed_bid"])
        else:
            entry_perp = float(x["perp_bid"])
            entry_fixed = float(x["fixed_ask"])

        funding_bps_h = x.get("funding_bps_per_hour")
        neutral_units = notional / max(entry_perp, entry_fixed)
        with sqlite3.connect(path) as con:
            cur = con.execute(
                """INSERT INTO rv_paper_pairs(
                    opened_ms,root,perp_symbol,fixed_symbol,direction,
                    notional_per_leg_czk,entry_perp_px,entry_fixed_px,
                    entry_edge_bps,entry_funding_bps_h,neutral_units,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                (
                    now, x["root"], x["perp_symbol"], x["fixed_symbol"], direction,
                    notional, entry_perp, entry_fixed,
                    float(x["net_basis_proxy_bps"]),
                    float(funding_bps_h) if funding_bps_h is not None else None,
                    neutral_units,
                ),
            )
            trade_id = int(cur.lastrowid)

        open_keys.add(key)
        recent_ms[key] = now
        opened.append({
            "id": trade_id,
            "root": x["root"],
            "perp_symbol": x["perp_symbol"],
            "fixed_symbol": x["fixed_symbol"],
            "direction": direction,
            "notional_per_leg_czk": round(notional, 2),
            "entry_edge_bps": float(x["net_basis_proxy_bps"]),
            "live_orders": False,
        })
    return opened


def paper_status(scan: dict[str, Any] | None = None, path: Path = DB_PATH) -> dict[str, Any]:
    init_db(path)
    qmap = _pair_map(scan or {})
    now = now_ms()
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT id,opened_ms,direction,notional_per_leg_czk,
                      entry_perp_px,entry_fixed_px,entry_funding_bps_h,neutral_units,
                      perp_symbol,fixed_symbol,root,entry_edge_bps
               FROM rv_paper_pairs WHERE status='OPEN'
               ORDER BY opened_ms"""
        ).fetchall()
        closed_n = int(con.execute(
            "SELECT COUNT(*) FROM rv_paper_pairs WHERE status='CLOSED'"
        ).fetchone()[0])
        recent = [
            {
                "id": r[0], "root": r[1], "direction": r[2], "status": r[3],
                "pnl_czk": r[4], "net_pnl_bps": r[5], "exit_reason": r[6],
            }
            for r in con.execute(
                """SELECT id,root,direction,status,pnl_czk,net_pnl_bps,exit_reason
                   FROM rv_paper_pairs ORDER BY id DESC LIMIT 10"""
            ).fetchall()
        ]

    open_marks: list[dict[str, Any]] = []
    unrealized = 0.0
    for r in rows:
        quote = qmap.get((str(r[8]), str(r[9])))
        mark = _paper_mark(r[:8], quote, now) if quote else None
        if mark:
            unrealized += float(mark["pnl_czk"])
        open_marks.append({
            "id": int(r[0]),
            "root": r[10],
            "perp_symbol": r[8],
            "fixed_symbol": r[9],
            "direction": r[2],
            "entry_edge_bps": round(float(r[11]), 4),
            "neutral_units": round(float(r[7] or 0.0), 8),
            "mark_net_pnl_bps": round(float(mark["net_pnl_bps"]), 4) if mark else None,
            "mark_pnl_czk": round(float(mark["pnl_czk"]), 4) if mark else None,
        })

    realized_equity = paper_equity(path)
    return {
        "enabled": PAPER_ENABLED,
        "realized_equity": round(realized_equity, 2),
        "marked_equity": round(realized_equity + unrealized, 2),
        "open_pairs": len(rows),
        "closed_pairs": closed_n,
        "max_open": PAPER_MAX_OPEN,
        "alloc_pct_per_leg": round(PAPER_ALLOC_PCT * 100.0, 2),
        "take_bps": PAPER_TAKE_BPS,
        "stop_bps": PAPER_STOP_BPS,
        "entry_z": ENTRY_Z,
        "exit_z": EXIT_Z,
        "stop_z": STOP_Z,
        "min_history": MIN_HISTORY,
        "max_hold_h": PAPER_MAX_HOLD_H,
        "open": open_marks,
        "recent": recent,
        "live_orders": False,
    }


class RelativeValueRuntime:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_scan: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_scan_ms: int | None = None

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, name="relative-value", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                scan = scan_opportunities(db_path=self.db_path, persist=True)
                closed = resolve_paper_pairs(scan, self.db_path)
                opened = maybe_open_paper_pairs(scan, self.db_path)
                scan["paper"] = paper_status(scan, self.db_path)
                scan["paper_opened_this_cycle"] = opened
                scan["paper_closed_this_cycle"] = closed
                self.last_scan = scan
                self.last_scan_ms = now_ms()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(SCAN_INTERVAL_S)

    def status(self) -> dict[str, Any]:
        total_rows = 0
        if self.db_path.exists():
            try:
                with sqlite3.connect(self.db_path) as con:
                    total_rows = int(con.execute("SELECT COUNT(*) FROM rv_snapshots").fetchone()[0])
            except Exception:
                total_rows = 0
        scan = self.last_scan or {}
        return {
            "running": bool(self.thread and self.thread.is_alive() and not self._stop.is_set()),
            "last_scan_ms": self.last_scan_ms,
            "last_error": self.last_error,
            "scan_interval_s": SCAN_INTERVAL_S,
            "pairs_scanned": int(scan.get("pairs_scanned", 0)),
            "eligible_count": int(scan.get("eligible_count", 0)),
            "best": scan.get("best", [])[:5],
            "paper": scan.get("paper", paper_status(scan, self.db_path)),
            "history_rows": total_rows,
            "live_orders": False,
        }


def selftest() -> dict[str, Any]:
    # Keep the synthetic maturity inside the scanner's configured max horizon.
    future_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=90)
    code = future_date.strftime("%y%m%d")
    ff = f"FF_XBTUSD_{code}"
    instruments = [
        {"symbol": "PF_XBTUSD", "tradeable": True},
        {"symbol": ff, "tradeable": True},
    ]
    tickers = {
        "PF_XBTUSD": {"symbol": "PF_XBTUSD", "bid": 100.0, "ask": 100.1, "fundingRate": 0.0001},
        ff: {"symbol": ff, "bid": 101.0, "ask": 101.1},
    }
    test_db = Path("data/relative_value_selftest.db")
    if test_db.exists():
        try:
            test_db.unlink()
        except Exception:
            pass
    init_db(test_db)
    with sqlite3.connect(test_db) as con:
        for i in range(MIN_HISTORY):
            con.execute(
                """INSERT INTO rv_snapshots(
                    ts_ms,root,perp_symbol,fixed_symbol,expiry,days_to_expiry,direction,
                    perp_bid,perp_ask,fixed_bid,fixed_ask,mid_basis_bps,executable_basis_bps,
                    fee_floor_bps,adverse_buffer_bps,net_basis_proxy_bps,
                    funding_rate_raw,funding_bps_per_hour,zscore,eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now_ms() - (MIN_HISTORY - i) * 10000,
                    "XBTUSD", "PF_XBTUSD", ff, future_date.isoformat(), 90.0,
                    "LONG_PERP_SHORT_FIXED", 100.0, 100.1, 100.35, 100.45,
                    30.0 + (i % 3) * 0.2, 20.0, 8.0, 4.0, 5.0,
                    0.001, 0.1, 0.0, 0,
                ),
            )
    tickers[ff] = {"symbol": ff, "bid": 101.0, "ask": 101.05}
    r = scan_opportunities(instruments, tickers, test_db, persist=True)
    opened = maybe_open_paper_pairs(r, test_db)
    ps = paper_status(r, test_db)
    ok = bool(
        r["pairs_scanned"] == 1
        and r["best"]
        and r["best"][0]["direction"] == "LONG_PERP_SHORT_FIXED"
        and opened
        and ps["open_pairs"] == 1
    )
    return {"ok": ok, "result": r, "paper_opened": opened, "paper_status": ps}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest(), indent=2, default=str))
