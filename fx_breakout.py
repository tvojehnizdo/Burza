from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("FX_BREAKOUT_DB", "data/gbpjpy_breakout.db"))
PAIR = os.getenv("FX_BREAKOUT_PAIR", "GBPJPY")
PIP = 0.01
MAX_ACCEPTABLE_SPREAD_PIPS = float(os.getenv("FX_MAX_SPREAD_PIPS", "3.0"))
START_EQUITY_CZK = float(os.getenv("FX_PAPER_START_EQUITY", "50000"))
RISK_PER_ATTEMPT_PCT = float(os.getenv("FX_RISK_PER_ATTEMPT_PCT", "1.0"))
MAX_ATTEMPTS_PER_EVENT = int(os.getenv("FX_MAX_ATTEMPTS_PER_EVENT", "3"))

# Rules are explicit research hypotheses, not claims of profitability.
TECH_DONCHIAN_BARS = int(os.getenv("FX_TECH_DONCHIAN_BARS", "20"))
TECH_BREAKOUT_BUFFER_PIPS = float(os.getenv("FX_TECH_BREAKOUT_BUFFER_PIPS", "5"))
STOP_PIPS = float(os.getenv("FX_STOP_PIPS", "40"))
BE_TRIGGER_PIPS = float(os.getenv("FX_BE_TRIGGER_PIPS", "120"))
TRAIL_TRIGGER_PIPS = float(os.getenv("FX_TRAIL_TRIGGER_PIPS", "150"))
TRAIL_DISTANCE_PIPS = float(os.getenv("FX_TRAIL_DISTANCE_PIPS", "45"))
MAX_HOLD_MIN = int(os.getenv("FX_MAX_HOLD_MIN", "60"))


def now_ms() -> int:
    return int(time.time() * 1000)


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS fx_quotes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                pair TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                spread_pips REAL NOT NULL,
                source TEXT NOT NULL,
                news_active INTEGER NOT NULL,
                news_event_id TEXT
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_fx_quotes_ts ON fx_quotes(pair,ts_ms)"
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS fx_news_events(
                event_id TEXT PRIMARY KEY,
                release_ms INTEGER NOT NULL,
                label TEXT,
                active_from_ms INTEGER NOT NULL,
                active_until_ms INTEGER NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS fx_paper_trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                event_id TEXT,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                side TEXT NOT NULL,
                entry_bid REAL,
                entry_ask REAL,
                exit_bid REAL,
                exit_ask REAL,
                entry_mid REAL NOT NULL,
                exit_mid REAL,
                risk_czk REAL NOT NULL,
                stop_pips REAL NOT NULL,
                mfe_pips REAL NOT NULL DEFAULT 0,
                mae_pips REAL NOT NULL DEFAULT 0,
                realized_pips REAL,
                realized_r REAL,
                spread_cost_pips REAL,
                slippage_pips REAL,
                pnl_czk REAL,
                exit_reason TEXT,
                status TEXT NOT NULL
            )"""
        )


def _spread_pips(bid: float, ask: float) -> float:
    return (ask - bid) / PIP


def ingest_quote(
    bid: float,
    ask: float,
    ts_ms: int | None = None,
    source: str = "broker",
    news_active: bool = False,
    news_event_id: str | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    if not all(math.isfinite(x) and x > 0 for x in (bid, ask)):
        raise ValueError("bid/ask must be positive finite numbers")
    if ask < bid:
        raise ValueError("ask cannot be below bid")
    ts = int(ts_ms or now_ms())
    spread = _spread_pips(bid, ask)
    init_db(path)
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT INTO fx_quotes(ts_ms,pair,bid,ask,spread_pips,source,news_active,news_event_id)
               VALUES(?,?,?,?,?,?,?,?)""",
            (ts, PAIR, float(bid), float(ask), float(spread), source, 1 if news_active else 0, news_event_id),
        )
    return {
        "ok": True,
        "pair": PAIR,
        "ts_ms": ts,
        "bid": bid,
        "ask": ask,
        "spread_pips": round(spread, 3),
        "spread_ok": spread <= MAX_ACCEPTABLE_SPREAD_PIPS,
        "news_active": bool(news_active),
        "news_event_id": news_event_id,
        "paper_orders_submitted": False,
        "live_orders": False,
    }


def arm_news_event(
    event_id: str,
    release_ms: int,
    label: str = "",
    window_before_min: int = 15,
    window_after_min: int = 15,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    if not event_id.strip():
        raise ValueError("event_id is required")
    release_ms = int(release_ms)
    before = int(window_before_min * 60_000)
    after = int(window_after_min * 60_000)
    init_db(path)
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT INTO fx_news_events(event_id,release_ms,label,active_from_ms,active_until_ms)
               VALUES(?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 release_ms=excluded.release_ms,label=excluded.label,
                 active_from_ms=excluded.active_from_ms,active_until_ms=excluded.active_until_ms""",
            (event_id, release_ms, label, release_ms - before, release_ms + after),
        )
    return {
        "ok": True,
        "event_id": event_id,
        "release_ms": release_ms,
        "window_before_min": window_before_min,
        "window_after_min": window_after_min,
        "live_orders": False,
    }


def _quote_stats(path: Path = DB_PATH) -> dict[str, Any]:
    init_db(path)
    with sqlite3.connect(path) as con:
        row = con.execute(
            """SELECT COUNT(*),MIN(ts_ms),MAX(ts_ms),AVG(spread_pips),
                      MAX(spread_pips),SUM(CASE WHEN spread_pips>? THEN 1 ELSE 0 END)
               FROM fx_quotes WHERE pair=?""",
            (MAX_ACCEPTABLE_SPREAD_PIPS, PAIR),
        ).fetchone()
        recent = con.execute(
            """SELECT ts_ms,bid,ask,spread_pips,source,news_active,news_event_id
               FROM fx_quotes WHERE pair=? ORDER BY id DESC LIMIT 5""",
            (PAIR,),
        ).fetchall()
    count = int(row[0] or 0)
    return {
        "quotes": count,
        "first_ts_ms": row[1],
        "last_ts_ms": row[2],
        "avg_spread_pips": round(float(row[3]), 3) if row[3] is not None else None,
        "max_spread_pips": round(float(row[4]), 3) if row[4] is not None else None,
        "spread_rejections": int(row[5] or 0),
        "recent": [
            {
                "ts_ms": r[0], "bid": r[1], "ask": r[2], "spread_pips": r[3],
                "source": r[4], "news_active": bool(r[5]), "news_event_id": r[6],
            }
            for r in recent
        ],
    }


def _trade_stats(branch: str, path: Path = DB_PATH) -> dict[str, Any]:
    init_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT pnl_czk,realized_r,mfe_pips,mae_pips,status,exit_reason
               FROM fx_paper_trades WHERE branch=?""",
            (branch,),
        ).fetchall()

    closed = [r for r in rows if r[4] == "CLOSED" and r[0] is not None]
    open_n = sum(1 for r in rows if r[4] == "OPEN")
    pnls = [float(r[0]) for r in closed]
    rs = [float(r[1]) for r in closed if r[1] is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else (None if not wins else 99.0)
    return {
        "branch": branch,
        "open_trades": open_n,
        "closed_trades": len(closed),
        "pnl_czk": round(sum(pnls), 2),
        "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
        "profit_factor": round(float(pf), 3) if pf is not None else None,
        "live_orders": False,
    }


def status(path: Path = DB_PATH) -> dict[str, Any]:
    q = _quote_stats(path)
    feed_ready = q["quotes"] >= 100 and q["last_ts_ms"] is not None
    return {
        "mode": "GBPJPY_BREAKOUT_LAB_PAPER",
        "pair": PAIR,
        "database": str(path),
        "feed": q,
        "feed_ready": feed_ready,
        "rules": {
            "technical": {
                "donchian_bars_15m": TECH_DONCHIAN_BARS,
                "breakout_buffer_pips": TECH_BREAKOUT_BUFFER_PIPS,
                "news_window_excluded": True,
            },
            "news": {
                "requires_explicit_news_event": True,
                "news_window_only": True,
            },
            "shared": {
                "risk_per_attempt_pct": RISK_PER_ATTEMPT_PCT,
                "max_attempts_per_event": MAX_ATTEMPTS_PER_EVENT,
                "stop_pips": STOP_PIPS,
                "be_trigger_pips": BE_TRIGGER_PIPS,
                "trail_trigger_pips": TRAIL_TRIGGER_PIPS,
                "trail_distance_pips": TRAIL_DISTANCE_PIPS,
                "max_hold_min": MAX_HOLD_MIN,
                "max_spread_pips": MAX_ACCEPTABLE_SPREAD_PIPS,
            },
        },
        "technical": _trade_stats("TECHNICAL", path),
        "news": _trade_stats("NEWS", path),
        "paper_execution_state": (
            "DATA_COLLECTION_ONLY"
            if not feed_ready
            else "FEED_READY_STRATEGY_EXECUTION_NOT_ARMED"
        ),
        "data_quality_note": (
            "No synthetic broker statistics are assumed. The lab requires observed GBP/JPY bid/ask "
            "quotes and explicit news timestamps. Slippage cannot be called 'real' without execution/fill data."
        ),
        "live_orders": False,
    }


def selftest() -> dict[str, Any]:
    test = Path("data/gbpjpy_breakout_selftest.db")
    if test.exists():
        try:
            test.unlink()
        except Exception:
            pass
    init_db(test)
    a = ingest_quote(198.100, 198.125, 1_000_000, "selftest", False, None, test)
    b = ingest_quote(198.110, 198.160, 1_001_000, "selftest", True, "BOE_TEST", test)
    e = arm_news_event("BOE_TEST", 1_001_000, "synthetic", 15, 15, test)
    s = status(test)
    ok = (
        a["spread_pips"] == 2.5
        and b["spread_pips"] == 5.0
        and s["feed"]["quotes"] == 2
        and s["feed"]["spread_rejections"] == 1
        and e["ok"]
        and not s["live_orders"]
    )
    return {"ok": ok, "status": s}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest(), indent=2, default=str))
