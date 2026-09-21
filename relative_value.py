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


def _history_zscore(perp: str, fixed: str, current: float, path: Path = DB_PATH) -> tuple[float | None, int]:
    init_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT executable_basis_bps
               FROM rv_snapshots
               WHERE perp_symbol=? AND fixed_symbol=?
               ORDER BY id DESC LIMIT ?""",
            (perp, fixed, HISTORY_WINDOW),
        ).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < 30:
        return None, len(vals)
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / max(len(vals) - 1, 1)
    sd = math.sqrt(var)
    return ((current - mean) / sd if sd > 1e-9 else 0.0), len(vals)


def _funding_bps_per_hour(ticker: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = _f(ticker.get("fundingRate"))
    if raw is None:
        raw = _f(ticker.get("fundingRatePrediction"))
    # Kraken relative funding is an hourly decimal rate. Guard against an
    # absolute USD funding value or malformed payload by refusing large values.
    if raw is None or abs(raw) > 0.01:
        return raw, None
    return raw, raw * 10000.0


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

        # Conservative immediate hedge economics using executable sides.
        rich_fixed = (fb / pa - 1.0) * 10000.0  # long perp ask, short fixed bid
        cheap_fixed = (pb / fa - 1.0) * 10000.0  # short perp bid, long fixed ask
        if rich_fixed >= cheap_fixed:
            direction = "LONG_PERP_SHORT_FIXED"
            executable = rich_fixed
        else:
            direction = "SHORT_PERP_LONG_FIXED"
            executable = cheap_fixed

        mid_basis = (fm / pm - 1.0) * 10000.0
        net_proxy = executable - fee_floor - ADVERSE_BUFFER_BPS
        funding_raw, funding_bph = _funding_bps_per_hour(pt)
        funding_effect = None
        if funding_bph is not None:
            # Positive funding: longs pay shorts.
            funding_effect = -funding_bph if direction.startswith("LONG_PERP") else funding_bph

        z, n_hist = _history_zscore(perp_symbol, fixed_symbol, executable, db_path)
        annualized_basis_pct = (mid_basis / 100.0) * (365.0 / days) if days > 0 else None
        eligible = bool(net_proxy >= MIN_NET_EDGE_BPS)

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
            "executable_basis_bps": round(executable, 4),
            "annualized_mid_basis_pct": round(annualized_basis_pct, 3) if annualized_basis_pct is not None else None,
            "maker_fee_floor_bps": round(fee_floor, 4),
            "adverse_buffer_bps": round(ADVERSE_BUFFER_BPS, 4),
            "net_basis_proxy_bps": round(net_proxy, 4),
            "funding_rate_raw": funding_raw,
            "funding_bps_per_hour": round(funding_bph, 6) if funding_bph is not None else None,
            "funding_effect_bps_1h_for_direction": round(funding_effect, 6) if funding_effect is not None else None,
            "history_n": n_hist,
            "zscore": round(z, 3) if z is not None else None,
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
        "best": rows[:10],
        "eligible": [x for x in rows if x["eligible"]][:20],
        "all": rows,
        "note": (
            "Basis/funding scanner only. It does not guarantee convergence or fills; "
            "perpetual funding, leg risk, liquidity and margin remain material risks."
        ),
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
                self.last_scan = scan_opportunities(db_path=self.db_path, persist=True)
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
    r = scan_opportunities(instruments, tickers, Path("data/relative_value_selftest.db"), persist=False)
    ok = bool(r["pairs_scanned"] == 1 and r["best"] and r["best"][0]["direction"] == "LONG_PERP_SHORT_FIXED")
    return {"ok": ok, "result": r}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest(), indent=2, default=str))
