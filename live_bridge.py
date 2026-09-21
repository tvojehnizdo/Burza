from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from microstructure import DB_PATH, connect_db, init_db
from kraken_live_control import account_snapshot, load_policy, place_spot_margin_order

PAIR_MAP = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "XRP/USD": "XRPUSD",
}

MIN_CLOSED = int(os.getenv("LIVE_MIN_PAPER_TRADES", "20"))
MIN_WIN_RATE = float(os.getenv("LIVE_MIN_PAPER_WIN_RATE", "0.52"))
MIN_NET_PNL = float(os.getenv("LIVE_MIN_PAPER_PNL_CZK", "1.0"))
POSITION_PCT = float(os.getenv("LIVE_POSITION_PCT_EQUITY", "10.0"))
LEVERAGE = int(os.getenv("LIVE_DEFAULT_LEVERAGE", "2"))
POLL_S = float(os.getenv("LIVE_BRIDGE_POLL_S", "2.0"))


def init_live_table() -> None:
    init_db(DB_PATH)
    with connect_db(DB_PATH) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_mirror(
                paper_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                leverage INTEGER NOT NULL,
                volume REAL NOT NULL,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                open_result TEXT,
                close_result TEXT,
                status TEXT NOT NULL,
                live_mode INTEGER NOT NULL DEFAULT 0
            );
            """
        )


def paper_stats() -> dict[str, Any]:
    init_live_table()
    with connect_db(DB_PATH) as con:
        rows = con.execute(
            """SELECT pnl_czk FROM paper_trades
               WHERE status='CLOSED' AND pnl_czk IS NOT NULL
               ORDER BY id ASC"""
        ).fetchall()
    pnls = [float(r[0]) for r in rows]
    wins = sum(p > 0 for p in pnls)
    return {
        "closed": len(pnls),
        "net_pnl_czk": round(sum(pnls), 4),
        "win_rate": (wins / len(pnls)) if pnls else 0.0,
        "gate": bool(
            len(pnls) >= MIN_CLOSED
            and sum(pnls) >= MIN_NET_PNL
            and (wins / len(pnls)) >= MIN_WIN_RATE
        ) if pnls else False,
        "requirements": {
            "min_closed": MIN_CLOSED,
            "min_net_pnl_czk": MIN_NET_PNL,
            "min_win_rate": MIN_WIN_RATE,
        },
    }


def _equity_usd() -> float:
    snap = account_snapshot()
    tb = snap.get("trade_balance") or {}
    for key in ("e", "tb"):
        try:
            v = float(tb.get(key))
            if v > 0:
                return v
        except Exception:
            pass
    return 0.0


def _open_paper_rows() -> list[tuple]:
    with connect_db(DB_PATH) as con:
        return con.execute(
            """SELECT id,opened_ms,symbol,side,entry
               FROM paper_trades WHERE status='OPEN' ORDER BY id ASC"""
        ).fetchall()


def _closed_paper_rows() -> list[tuple]:
    with connect_db(DB_PATH) as con:
        return con.execute(
            """SELECT p.id,p.closed_ms,p.symbol,p.side,p.entry,l.volume,l.leverage
               FROM paper_trades p
               JOIN live_mirror l ON l.paper_id=p.id
               WHERE p.status='CLOSED' AND l.status='OPEN'
               ORDER BY p.id ASC"""
        ).fetchall()


def mirror_open() -> list[dict[str, Any]]:
    stats = paper_stats()
    policy = load_policy()
    results: list[dict[str, Any]] = []
    if not stats["gate"]:
        return results

    equity = _equity_usd()
    if equity <= 0:
        return results
    target_notional = equity * POSITION_PCT / 100.0

    for paper_id, opened_ms, symbol, side, entry in _open_paper_rows():
        pair = PAIR_MAP.get(symbol)
        if not pair:
            continue
        with connect_db(DB_PATH) as con:
            exists = con.execute("SELECT 1 FROM live_mirror WHERE paper_id=?", (paper_id,)).fetchone()
        if exists:
            continue

        volume = target_notional / float(entry)
        order_side = "buy" if side == "LONG" else "sell"
        response = place_spot_margin_order(
            pair=pair,
            side=order_side,
            volume=volume,
            leverage=LEVERAGE,
            ordertype="market",
            force_validate=False,
        )
        live_mode = 1 if response.get("submitted_live") else 0
        with connect_db(DB_PATH) as con:
            con.execute(
                """INSERT INTO live_mirror(
                    paper_id,symbol,pair,side,leverage,volume,opened_ms,
                    open_result,status,live_mode
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id, symbol, pair, side, LEVERAGE, volume, int(opened_ms),
                    json.dumps(response, default=str), "OPEN", live_mode,
                ),
            )
        results.append({"paper_id": paper_id, "action": "OPEN", "result": response})
    return results


def mirror_close() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for paper_id, closed_ms, symbol, side, entry, volume, leverage in _closed_paper_rows():
        pair = PAIR_MAP.get(symbol)
        if not pair:
            continue
        close_side = "sell" if side == "LONG" else "buy"
        response = place_spot_margin_order(
            pair=pair,
            side=close_side,
            volume=float(volume),
            leverage=int(leverage),
            ordertype="market",
            force_validate=False,
            reduce_only=True,
        )
        with connect_db(DB_PATH) as con:
            con.execute(
                """UPDATE live_mirror
                   SET closed_ms=?, close_result=?, status='CLOSED'
                   WHERE paper_id=?""",
                (int(closed_ms or time.time()*1000), json.dumps(response, default=str), paper_id),
            )
        results.append({"paper_id": paper_id, "action": "CLOSE", "result": response})
    return results


class LiveBridge:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last: dict[str, Any] | None = None
        self.error: str | None = None

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        init_live_table()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="live-bridge")
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                closed = mirror_close()
                opened = mirror_open()
                self.last = {
                    "time": int(time.time()),
                    "paper_stats": paper_stats(),
                    "policy": load_policy(),
                    "opened": opened,
                    "closed": closed,
                }
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(POLL_S)

    def status(self) -> dict[str, Any]:
        init_live_table()
        with connect_db(DB_PATH) as con:
            rows = con.execute(
                """SELECT paper_id,symbol,pair,side,status,live_mode
                   FROM live_mirror ORDER BY paper_id DESC LIMIT 20"""
            ).fetchall()
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "error": self.error,
            "paper_stats": paper_stats(),
            "policy": load_policy(),
            "recent": [
                {
                    "paper_id": r[0], "symbol": r[1], "pair": r[2],
                    "side": r[3], "status": r[4], "live_mode": bool(r[5]),
                }
                for r in rows
            ],
        }


BRIDGE = LiveBridge()
