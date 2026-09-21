from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from microstructure import DB_PATH, connect_db, get_meta, init_db, now_ms, set_meta

START_CAPITAL = float(os.getenv("START_CAPITAL", "5000"))
MAX_DD_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "10"))
ALPHA_INTERVAL_S = int(os.getenv("ALPHA_INTERVAL_S", "30"))
ALPHA_COST_BPS = float(os.getenv("V4_EXEC_ROUNDTRIP_BPS", "14"))
ALPHA_MIN_TRAIN = int(os.getenv("ALPHA_MIN_TRAIN", "40"))
ALPHA_MIN_VALID = int(os.getenv("ALPHA_MIN_VALID", "18"))
ALPHA_MIN_NET_BPS = float(os.getenv("ALPHA_MIN_NET_BPS", "2.0"))
ALPHA_MAX_SPREAD_BPS = float(os.getenv("ALPHA_MAX_SPREAD_BPS", "8"))
PAPER_ALLOC_PCT = float(os.getenv("PAPER_ALLOC_PCT", "25")) / 100.0
MAX_ROWS = int(os.getenv("ALPHA_MAX_ROWS", "150000"))


def ternary(value: float, threshold: float) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def state_key_from_row(row: pd.Series | dict[str, Any]) -> str:
    get = row.get
    obi = ternary(float(get("obi5", 0.0)), 0.15)
    flow = ternary(float(get("flow10", 0.0)), 0.15)
    pressure = ternary(float(get("pressure_bps", 0.0)), 0.30)
    ret = ternary(float(get("ret5_bps", 0.0)), 1.50)
    btc = ternary(float(get("btc_ret5_bps", 0.0)), 1.50)
    f = float(get("flow10", 0.0))
    r = float(get("ret5_bps", 0.0))
    # Pressure without movement: aggressive flow that price has not yet followed.
    absorption = ternary(f, 0.35) if abs(r) < 1.25 else 0
    vol = 1 if float(get("vol10_bps", 0.0)) > 3.0 else 0
    return f"o{obi}|f{flow}|m{pressure}|r{ret}|b{btc}|a{absorption}|v{vol}"


def load_snapshots(db_path: Path = DB_PATH, max_rows: int = MAX_ROWS) -> pd.DataFrame:
    init_db(db_path)
    with connect_db(db_path) as con:
        df = pd.read_sql_query(
            """SELECT * FROM (
                   SELECT * FROM micro_snapshots ORDER BY id DESC LIMIT ?
               ) ORDER BY ts_ms ASC""",
            con,
            params=(max_rows,),
        )
    return df


def enrich_states(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    x = df.sort_values(["symbol", "ts_ms"]).copy()
    btc = x[x.symbol == "BTC/USD"][["ts_ms", "ret5_bps"]].copy()
    btc = btc.rename(columns={"ret5_bps": "btc_ret5_bps"}).sort_values("ts_ms")
    if btc.empty:
        x["btc_ret5_bps"] = 0.0
    else:
        parts = []
        for symbol, g in x.groupby("symbol", sort=False):
            g = g.sort_values("ts_ms")
            if symbol == "BTC/USD":
                g["btc_ret5_bps"] = g["ret5_bps"].astype(float)
            else:
                g = pd.merge_asof(
                    g,
                    btc,
                    on="ts_ms",
                    direction="backward",
                    tolerance=3_000,
                )
                g["btc_ret5_bps"] = g["btc_ret5_bps"].fillna(0.0)
            parts.append(g)
        x = pd.concat(parts, ignore_index=True)
    x["relative_ret5_bps"] = x["ret5_bps"].astype(float) - x["btc_ret5_bps"].astype(float)
    x["state_key"] = x.apply(state_key_from_row, axis=1)
    return x.sort_values("ts_ms").reset_index(drop=True)


def add_forward_label(df: pd.DataFrame, horizon_s: int) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = []
    horizon_ms = horizon_s * 1000
    for symbol, g in df.groupby("symbol", sort=False):
        g = g.sort_values("ts_ms").copy()
        ts = g["ts_ms"].to_numpy(dtype=np.int64)
        mid = g["mid"].to_numpy(dtype=float)
        target = ts + horizon_ms
        pos = np.searchsorted(ts, target, side="left")
        fwd = np.full(len(g), np.nan)
        for i, j in enumerate(pos):
            if j < len(g) and ts[j] <= target[i] + 3_500 and mid[i] > 0:
                fwd[i] = (mid[j] / mid[i] - 1.0) * 10000.0
        g["fwd_bps"] = fwd
        out.append(g)
    return pd.concat(out, ignore_index=True).dropna(subset=["fwd_bps"])


def _side_metrics(values: np.ndarray, side: str, cost_bps: float) -> dict[str, float]:
    sign = 1.0 if side == "LONG" else -1.0
    signed = sign * values
    net = signed - cost_bps
    return {
        "n": int(len(values)),
        "gross_mean_bps": float(np.mean(signed)) if len(values) else 0.0,
        "net_mean_bps": float(np.mean(net)) if len(values) else 0.0,
        "net_median_bps": float(np.median(net)) if len(values) else 0.0,
        "hit_rate": float(np.mean(net > 0)) if len(values) else 0.0,
        "stdev_bps": float(np.std(signed, ddof=1)) if len(values) > 1 else 0.0,
    }


def discover_dataframe(
    raw: pd.DataFrame,
    horizon_s: int = 30,
    cost_bps: float = ALPHA_COST_BPS,
    min_train: int = ALPHA_MIN_TRAIN,
    min_valid: int = ALPHA_MIN_VALID,
    min_net_bps: float = ALPHA_MIN_NET_BPS,
) -> dict[str, Any]:
    x = enrich_states(raw)
    x = add_forward_label(x, horizon_s)
    x = x[x["spread_bps"].astype(float) <= ALPHA_MAX_SPREAD_BPS].copy()
    if len(x) < min_train + min_valid:
        return {
            "horizon_s": horizon_s,
            "rows": len(x),
            "models": [],
            "reason": "WARMUP",
        }

    cutoff = int(x["ts_ms"].quantile(0.70))
    train = x[x.ts_ms <= cutoff]
    valid = x[x.ts_ms > cutoff]
    models: list[dict[str, Any]] = []

    for (symbol, state), tg in train.groupby(["symbol", "state_key"]):
        if len(tg) < min_train:
            continue
        vals = tg["fwd_bps"].to_numpy(dtype=float)
        train_mean = float(np.mean(vals))
        side = "LONG" if train_mean >= 0 else "SHORT"
        tm = _side_metrics(vals, side, cost_bps)

        vg = valid[(valid.symbol == symbol) & (valid.state_key == state)]
        if len(vg) < min_valid:
            continue
        vm = _side_metrics(vg["fwd_bps"].to_numpy(dtype=float), side, cost_bps)

        same_direction = tm["gross_mean_bps"] > 0 and vm["gross_mean_bps"] > 0
        if not same_direction:
            continue
        if tm["net_mean_bps"] < min_net_bps or vm["net_mean_bps"] < min_net_bps:
            continue
        if tm["hit_rate"] < 0.53 or vm["hit_rate"] < 0.52:
            continue

        n_eff = min(tm["n"], vm["n"])
        noise = max(tm["stdev_bps"], vm["stdev_bps"], 1.0)
        robust_edge = min(tm["net_mean_bps"], vm["net_mean_bps"])
        score = robust_edge * math.sqrt(n_eff) / noise
        models.append({
            "symbol": symbol,
            "state_key": state,
            "side": side,
            "horizon_s": horizon_s,
            "score": round(float(score), 5),
            "robust_edge_bps": round(float(robust_edge), 4),
            "train": {k: round(v, 5) if isinstance(v, float) else v for k, v in tm.items()},
            "validation": {k: round(v, 5) if isinstance(v, float) else v for k, v in vm.items()},
        })

    models.sort(key=lambda z: (z["score"], z["robust_edge_bps"]), reverse=True)
    return {
        "horizon_s": horizon_s,
        "rows": len(x),
        "train_rows": len(train),
        "validation_rows": len(valid),
        "models": models[:100],
        "reason": "OK" if models else "NO_VALIDATED_EDGE",
    }


def discover_models(db_path: Path = DB_PATH) -> dict[str, Any]:
    raw = load_snapshots(db_path)
    result = {
        "generated_ms": now_ms(),
        "rows": len(raw),
        "cost_bps": ALPHA_COST_BPS,
        "horizons": {},
        "consensus": [],
    }
    for h in (30, 60):
        result["horizons"][str(h)] = discover_dataframe(raw, h)

    m30 = {
        (m["symbol"], m["state_key"]): m
        for m in result["horizons"]["30"]["models"]
    }
    m60 = {
        (m["symbol"], m["state_key"]): m
        for m in result["horizons"]["60"]["models"]
    }
    consensus = []
    for key, a in m30.items():
        b = m60.get(key)
        if not b or a["side"] != b["side"]:
            continue
        consensus.append({
            "symbol": a["symbol"],
            "state_key": a["state_key"],
            "side": a["side"],
            "score": round(min(a["score"], b["score"]), 5),
            "edge30_bps": a["robust_edge_bps"],
            "edge60_bps": b["robust_edge_bps"],
            "n30_valid": a["validation"]["n"],
            "n60_valid": b["validation"]["n"],
        })
    consensus.sort(key=lambda z: (z["score"], min(z["edge30_bps"], z["edge60_bps"])), reverse=True)
    result["consensus"] = consensus[:50]
    return result


def latest_rows(db_path: Path = DB_PATH) -> pd.DataFrame:
    with connect_db(db_path) as con:
        df = pd.read_sql_query(
            """SELECT m.* FROM micro_snapshots m
               JOIN (SELECT symbol, MAX(ts_ms) ts FROM micro_snapshots GROUP BY symbol) z
               ON m.symbol=z.symbol AND m.ts_ms=z.ts
               ORDER BY m.symbol""",
            con,
        )
    return enrich_states(df)


def paper_equity(db_path: Path = DB_PATH) -> float:
    value = get_meta("paper_equity", START_CAPITAL, db_path)
    try:
        return float(value)
    except Exception:
        return START_CAPITAL


def resolve_paper(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    t = now_ms()
    closed = []
    with connect_db(db_path) as con:
        rows = con.execute(
            """SELECT id,opened_ms,symbol,side,horizon_s,entry,notional_czk,cost_bps
               FROM paper_trades WHERE status='OPEN' AND opened_ms + horizon_s*1000 <= ?""",
            (t,),
        ).fetchall()
        for row in rows:
            trade_id, opened, symbol, side, horizon, entry, notional, cost_bps = row
            px = con.execute(
                """SELECT mid,ts_ms FROM micro_snapshots
                   WHERE symbol=? AND ts_ms>=? ORDER BY ts_ms ASC LIMIT 1""",
                (symbol, opened + horizon * 1000),
            ).fetchone()
            if not px:
                continue
            exit_px, closed_ms = float(px[0]), int(px[1])
            sign = 1.0 if side == "LONG" else -1.0
            gross_bps = sign * (exit_px / float(entry) - 1.0) * 10000.0
            net_bps = gross_bps - float(cost_bps)
            pnl = float(notional) * net_bps / 10000.0
            con.execute(
                """UPDATE paper_trades
                   SET closed_ms=?,exit=?,pnl_czk=?,net_bps=?,status='CLOSED'
                   WHERE id=?""",
                (closed_ms, exit_px, pnl, net_bps, trade_id),
            )
            closed.append({
                "id": trade_id, "symbol": symbol, "side": side,
                "net_bps": round(net_bps, 3), "pnl_czk": round(pnl, 3),
            })

    if closed:
        eq = paper_equity(db_path) + sum(x["pnl_czk"] for x in closed)
        set_meta("paper_equity", eq, db_path)
    return closed


def maybe_open_paper(models: dict[str, Any], db_path: Path = DB_PATH) -> dict[str, Any] | None:
    consensus = {
        (x["symbol"], x["state_key"]): x for x in models.get("consensus", [])
    }
    if not consensus:
        return None

    eq = paper_equity(db_path)
    if eq <= START_CAPITAL * (1.0 - MAX_DD_PCT / 100.0):
        set_meta("paper_halted", {"reason": "MAX_DRAWDOWN", "equity": eq}, db_path)
        return None

    with connect_db(db_path) as con:
        if con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0] > 0:
            return None

    latest = latest_rows(db_path)
    candidates = []
    t = now_ms()
    for _, row in latest.iterrows():
        if t - int(row.ts_ms) > 5_000:
            continue
        key = (row.symbol, row.state_key)
        model = consensus.get(key)
        if not model:
            continue
        if float(row.spread_bps) > ALPHA_MAX_SPREAD_BPS:
            continue
        candidates.append((model["score"], model, row))

    if not candidates:
        return None
    candidates.sort(key=lambda z: z[0], reverse=True)
    _, model, row = candidates[0]
    notional = min(eq * PAPER_ALLOC_PCT, eq)
    with connect_db(db_path) as con:
        cur = con.execute(
            """INSERT INTO paper_trades(
                opened_ms,symbol,side,horizon_s,entry,notional_czk,
                model_edge_bps,model_score,cost_bps,state_key,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
            (
                t, row.symbol, model["side"], 30, float(row.mid), notional,
                min(model["edge30_bps"], model["edge60_bps"]),
                model["score"], ALPHA_COST_BPS, row.state_key,
            ),
        )
        trade_id = cur.lastrowid
    return {
        "id": trade_id, "symbol": row.symbol, "side": model["side"],
        "entry": float(row.mid), "notional_czk": round(notional, 2),
        "edge_bps": min(model["edge30_bps"], model["edge60_bps"]),
        "score": model["score"], "state_key": row.state_key,
    }


class AlphaRuntime:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_models: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_cycle_ms: int | None = None

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, name="alpha-discovery", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                closed = resolve_paper(self.db_path)
                models = discover_models(self.db_path)
                opened = maybe_open_paper(models, self.db_path)
                self.last_models = models
                self.last_cycle_ms = now_ms()
                set_meta("alpha_last", {
                    "generated_ms": self.last_cycle_ms,
                    "rows": models["rows"],
                    "consensus_count": len(models["consensus"]),
                    "opened": opened,
                    "closed": closed,
                }, self.db_path)
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                set_meta("alpha_error", self.last_error, self.db_path)
            self._stop.wait(ALPHA_INTERVAL_S)

    def status(self) -> dict[str, Any]:
        init_db(self.db_path)
        with connect_db(self.db_path) as con:
            open_n = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
            closed_n = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='CLOSED'").fetchone()[0]
            rows = con.execute("SELECT COUNT(*) FROM micro_snapshots").fetchone()[0]
            recent = [
                {
                    "id": r[0], "symbol": r[1], "side": r[2], "status": r[3],
                    "pnl_czk": r[4], "net_bps": r[5],
                }
                for r in con.execute(
                    """SELECT id,symbol,side,status,pnl_czk,net_bps
                       FROM paper_trades ORDER BY id DESC LIMIT 10"""
                ).fetchall()
            ]
        return {
            "running": bool(self.thread and self.thread.is_alive() and not self._stop.is_set()),
            "last_cycle_ms": self.last_cycle_ms,
            "last_error": self.last_error,
            "rows": rows,
            "paper_equity": round(paper_equity(self.db_path), 2),
            "open_trades": open_n,
            "closed_trades": closed_n,
            "consensus_count": len(self.last_models.get("consensus", [])) if self.last_models else 0,
            "recent_paper": recent,
            "live_orders": False,
        }


def alpha_selftest() -> dict[str, Any]:
    # A deterministic toy market where one microstructure state has a repeatable
    # +6 bps 30s move. Discovery should find it after train/validation split.
    rows = []
    n = 900
    base_ts = 1_700_000_000_000
    mids = np.full(n, 100.0)
    pulse_idx = set(range(50, 820, 20))
    for i in range(n):
        if i in pulse_idx:
            for j in range(i + 1, min(i + 31, n)):
                mids[j] += 0.0002 * (j - i)
    for i in range(n):
        pulse = i in pulse_idx
        rows.append({
            "id": i + 1, "ts_ms": base_ts + i * 1000, "symbol": "BTC/USD",
            "bid": mids[i] - 0.005, "ask": mids[i] + 0.005, "mid": mids[i],
            "spread_bps": 1.0, "microprice": mids[i] + (0.004 if pulse else 0),
            "pressure_bps": 0.4 if pulse else 0.0,
            "obi5": 0.5 if pulse else 0.0, "obi10": 0.4 if pulse else 0.0,
            "bid_depth5": 10, "ask_depth5": 5,
            "buy_vol10": 10 if pulse else 1, "sell_vol10": 2 if pulse else 1,
            "flow10": 0.67 if pulse else 0.0, "trades10": 5,
            "buy_vol30": 12, "sell_vol30": 4, "flow30": 0.5, "trades30": 10,
            "ret1_bps": 0.0, "ret5_bps": 0.0, "vol10_bps": 0.5,
        })
    df = pd.DataFrame(rows)
    result = discover_dataframe(df, horizon_s=30, cost_bps=0.0, min_train=8, min_valid=3, min_net_bps=0.2)
    found = any(m["side"] == "LONG" for m in result["models"])
    return {"ok": found, "models": result["models"][:3], "rows": result["rows"]}


if __name__ == "__main__":
    print(json.dumps(alpha_selftest(), indent=2, default=str))
