from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import websockets

WS_URL = os.getenv("KRAKEN_WS_V2", "wss://ws.kraken.com/v2")
WS_SYMBOLS = [s.strip() for s in os.getenv(
    "WS_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,XRP/USD"
).split(",") if s.strip()]
BOOK_DEPTH = int(os.getenv("BOOK_DEPTH", "10"))
SNAPSHOT_MS = int(os.getenv("MICRO_SNAPSHOT_MS", "1000"))
DB_PATH = Path(os.getenv("DATA_DB", "data/pulse_v4.db"))


def now_ms() -> int:
    return int(time.time() * 1000)


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(path: Path = DB_PATH) -> None:
    with connect_db(path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS micro_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                mid REAL NOT NULL,
                spread_bps REAL NOT NULL,
                microprice REAL NOT NULL,
                pressure_bps REAL NOT NULL,
                obi5 REAL NOT NULL,
                obi10 REAL NOT NULL,
                bid_depth5 REAL NOT NULL,
                ask_depth5 REAL NOT NULL,
                buy_vol10 REAL NOT NULL,
                sell_vol10 REAL NOT NULL,
                flow10 REAL NOT NULL,
                trades10 INTEGER NOT NULL,
                buy_vol30 REAL NOT NULL,
                sell_vol30 REAL NOT NULL,
                flow30 REAL NOT NULL,
                trades30 INTEGER NOT NULL,
                ret1_bps REAL NOT NULL,
                ret5_bps REAL NOT NULL,
                vol10_bps REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_micro_symbol_ts
                ON micro_snapshots(symbol, ts_ms);

            CREATE TABLE IF NOT EXISTS paper_trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                horizon_s INTEGER NOT NULL,
                entry REAL NOT NULL,
                exit REAL,
                notional_czk REAL NOT NULL,
                model_edge_bps REAL NOT NULL,
                model_score REAL NOT NULL,
                cost_bps REAL NOT NULL,
                pnl_czk REAL,
                net_bps REAL,
                state_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN'
            );
            CREATE INDEX IF NOT EXISTS idx_paper_status
                ON paper_trades(status, closed_ms);

            CREATE TABLE IF NOT EXISTS shadow_paper_trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                horizon_s INTEGER NOT NULL,
                entry REAL NOT NULL,
                exit REAL,
                notional_czk REAL NOT NULL,
                signal_edge_bps REAL NOT NULL,
                signal_score REAL NOT NULL,
                cost_bps REAL NOT NULL,
                pnl_czk REAL,
                net_bps REAL,
                state_key TEXT NOT NULL,
                signal_kind TEXT NOT NULL DEFAULT 'UNVALIDATED_STATE',
                status TEXT NOT NULL DEFAULT 'OPEN'
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_paper_status
                ON shadow_paper_trades(status, closed_ms);

            CREATE TABLE IF NOT EXISTS runtime_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            """
        )


def set_meta(key: str, value: Any, path: Path = DB_PATH) -> None:
    init_db(path)
    payload = value if isinstance(value, str) else json.dumps(value, default=str)
    with connect_db(path) as con:
        con.execute(
            """INSERT INTO runtime_meta(key,value,updated_ms) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms""",
            (key, payload, now_ms()),
        )


def get_meta(key: str, default: Any = None, path: Path = DB_PATH) -> Any:
    init_db(path)
    with connect_db(path) as con:
        row = con.execute("SELECT value FROM runtime_meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


class KrakenMicroRecorder:
    """Kraken Spot WebSocket v2 L2 + trades recorder.

    This recorder never authenticates and cannot place orders. It stores compact
    1-second microstructure snapshots used by the V4 alpha discovery layer.
    """

    def __init__(self, symbols: list[str] | None = None, db_path: Path = DB_PATH):
        self.symbols = symbols or WS_SYMBOLS
        self.db_path = db_path
        self.books: dict[str, dict[str, dict[float, float]]] = {
            s: {"bids": {}, "asks": {}} for s in self.symbols
        }
        self.trade_tape: dict[str, deque[tuple[int, str, float]]] = {
            s: deque(maxlen=20000) for s in self.symbols
        }
        self.mid_hist: dict[str, deque[tuple[int, float]]] = {
            s: deque(maxlen=120) for s in self.symbols
        }
        self.last_persist: dict[str, int] = defaultdict(int)
        self.last_message_ms = 0
        self.last_error: str | None = None
        self.reconnects = 0
        self.running = False
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        init_db(self.db_path)

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self._stop.clear()
        self.running = True
        self.thread = threading.Thread(target=self._thread_main, name="kraken-micro-recorder", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def status(self) -> dict[str, Any]:
        age = None if not self.last_message_ms else max(0, now_ms() - self.last_message_ms)
        with connect_db(self.db_path) as con:
            count = con.execute("SELECT COUNT(*) FROM micro_snapshots").fetchone()[0]
            last = con.execute("SELECT MAX(ts_ms) FROM micro_snapshots").fetchone()[0]
        return {
            "running": bool(self.thread and self.thread.is_alive() and not self._stop.is_set()),
            "symbols": self.symbols,
            "rows": count,
            "last_snapshot_ms": last,
            "last_message_age_ms": age,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "db": str(self.db_path),
            "live_orders": False,
        }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.running = False

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=4_000_000,
                ) as ws:
                    self.reconnects += 1
                    self.last_error = None
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "book",
                            "symbol": self.symbols,
                            "depth": BOOK_DEPTH,
                            "snapshot": True,
                        },
                        "req_id": 1,
                    }))
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "trade",
                            "symbol": self.symbols,
                            "snapshot": False,
                        },
                        "req_id": 2,
                    }))
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=40)
                        self.last_message_ms = now_ms()
                        msg = json.loads(raw)
                        self.process_message(msg)
            except asyncio.TimeoutError:
                self.last_error = "WebSocket stale >40s; reconnecting"
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 20.0)
        self.running = False

    def process_message(self, msg: dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "book":
            self._process_book(msg)
        elif channel == "trade":
            self._process_trades(msg)

    @staticmethod
    def _apply_levels(dst: dict[float, float], levels: list[dict[str, Any]]) -> None:
        for level in levels or []:
            price = float(level["price"])
            qty = float(level["qty"])
            if qty == 0.0:
                dst.pop(price, None)
            else:
                dst[price] = qty

    def _process_book(self, msg: dict[str, Any]) -> None:
        typ = msg.get("type")
        for item in msg.get("data") or []:
            symbol = item.get("symbol")
            if symbol not in self.books:
                continue
            if typ == "snapshot":
                self.books[symbol]["bids"].clear()
                self.books[symbol]["asks"].clear()
            self._apply_levels(self.books[symbol]["bids"], item.get("bids") or [])
            self._apply_levels(self.books[symbol]["asks"], item.get("asks") or [])
            self._maybe_snapshot(symbol)

    def _process_trades(self, msg: dict[str, Any]) -> None:
        t = now_ms()
        for tr in msg.get("data") or []:
            symbol = tr.get("symbol")
            if symbol not in self.trade_tape:
                continue
            side = str(tr.get("side", "")).lower()
            qty = float(tr.get("qty", 0.0) or 0.0)
            if side in {"buy", "sell"} and qty > 0:
                self.trade_tape[symbol].append((t, side, qty))

    @staticmethod
    def _at_or_before(hist: deque[tuple[int, float]], target_ms: int) -> float | None:
        for ts, value in reversed(hist):
            if ts <= target_ms:
                return value
        return None

    def _flow(self, symbol: str, window_ms: int, t: int) -> tuple[float, float, float, int]:
        tape = self.trade_tape[symbol]
        cutoff = t - window_ms
        while tape and tape[0][0] < t - 120_000:
            tape.popleft()
        buys = sum(q for ts, side, q in tape if ts >= cutoff and side == "buy")
        sells = sum(q for ts, side, q in tape if ts >= cutoff and side == "sell")
        total = buys + sells
        delta = (buys - sells) / total if total > 0 else 0.0
        count = sum(1 for ts, _, _ in tape if ts >= cutoff)
        return buys, sells, delta, count

    def _maybe_snapshot(self, symbol: str) -> None:
        t = now_ms()
        if t - self.last_persist[symbol] < SNAPSHOT_MS:
            return
        book = self.books[symbol]
        if not book["bids"] or not book["asks"]:
            return

        bids = sorted(book["bids"].items(), reverse=True)[:BOOK_DEPTH]
        asks = sorted(book["asks"].items())[:BOOK_DEPTH]
        if not bids or not asks:
            return
        bid, bidq = bids[0]
        ask, askq = asks[0]
        if bid <= 0 or ask <= bid:
            return

        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10000.0
        denom = bidq + askq
        micro = (ask * bidq + bid * askq) / denom if denom > 0 else mid
        pressure_bps = (micro - mid) / mid * 10000.0

        b5 = sum(q for _, q in bids[:5])
        a5 = sum(q for _, q in asks[:5])
        b10 = sum(q for _, q in bids[:10])
        a10 = sum(q for _, q in asks[:10])
        obi5 = (b5 - a5) / (b5 + a5) if b5 + a5 > 0 else 0.0
        obi10 = (b10 - a10) / (b10 + a10) if b10 + a10 > 0 else 0.0

        buy10, sell10, flow10, trades10 = self._flow(symbol, 10_000, t)
        buy30, sell30, flow30, trades30 = self._flow(symbol, 30_000, t)

        hist = self.mid_hist[symbol]
        p1 = self._at_or_before(hist, t - 1_000)
        p5 = self._at_or_before(hist, t - 5_000)
        ret1 = ((mid / p1) - 1.0) * 10000.0 if p1 else 0.0
        ret5 = ((mid / p5) - 1.0) * 10000.0 if p5 else 0.0
        recent = [v for ts, v in hist if ts >= t - 10_000]
        if len(recent) >= 3:
            arr = [math.log(recent[i] / recent[i - 1]) * 10000.0 for i in range(1, len(recent)) if recent[i - 1] > 0]
            vol10 = float((sum((x - sum(arr)/len(arr))**2 for x in arr) / max(len(arr)-1, 1)) ** 0.5) if arr else 0.0
        else:
            vol10 = 0.0
        hist.append((t, mid))

        row = (
            t, symbol, bid, ask, mid, spread_bps, micro, pressure_bps,
            obi5, obi10, b5, a5, buy10, sell10, flow10, trades10,
            buy30, sell30, flow30, trades30, ret1, ret5, vol10
        )
        with connect_db(self.db_path) as con:
            con.execute(
                """INSERT INTO micro_snapshots(
                    ts_ms,symbol,bid,ask,mid,spread_bps,microprice,pressure_bps,
                    obi5,obi10,bid_depth5,ask_depth5,buy_vol10,sell_vol10,flow10,trades10,
                    buy_vol30,sell_vol30,flow30,trades30,ret1_bps,ret5_bps,vol10_bps
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        self.last_persist[symbol] = t


def recorder_selftest() -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.db"
        rec = KrakenMicroRecorder(["BTC/USD"], path)
        rec.process_message({
            "channel": "book", "type": "snapshot",
            "data": [{
                "symbol": "BTC/USD",
                "bids": [{"price": 100.0, "qty": 5.0}, {"price": 99.0, "qty": 3.0}],
                "asks": [{"price": 101.0, "qty": 2.0}, {"price": 102.0, "qty": 4.0}],
            }]
        })
        rec.process_message({
            "channel": "trade", "type": "update",
            "data": [
                {"symbol": "BTC/USD", "side": "buy", "qty": 2.0, "price": 101.0},
                {"symbol": "BTC/USD", "side": "sell", "qty": 1.0, "price": 100.0},
            ]
        })
        rec.last_persist["BTC/USD"] = 0
        rec.process_message({
            "channel": "book", "type": "update",
            "data": [{"symbol": "BTC/USD", "bids": [{"price": 100.0, "qty": 6.0}], "asks": []}]
        })
        with connect_db(path) as con:
            row = con.execute(
                "SELECT mid,spread_bps,obi5,flow10 FROM micro_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        ok = bool(row and row[0] == 100.5 and row[1] > 0 and row[2] > 0 and row[3] > 0)
        return {"ok": ok, "row": row}


if __name__ == "__main__":
    print(json.dumps(recorder_selftest(), indent=2))
