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
EXECUTION_MODE = os.getenv("V4_EXECUTION_MODE", "market_taker").strip().lower()
KRAKEN_MAKER_FEE_BPS = float(os.getenv("KRAKEN_MAKER_BPS", "40"))
KRAKEN_TAKER_FEE_BPS = float(os.getenv("KRAKEN_TAKER_BPS", "80"))
SLIPPAGE_BPS = float(os.getenv("SLIPPAGE_BPS", "2"))
EXECUTION_PENALTY_BPS = float(os.getenv("EXECUTION_PENALTY_BPS", "3"))
DEFAULT_ROUNDTRIP_COST_BPS = (
    2.0 * (KRAKEN_TAKER_FEE_BPS if EXECUTION_MODE == "market_taker" else KRAKEN_MAKER_FEE_BPS)
    + 2.0 * SLIPPAGE_BPS
    + EXECUTION_PENALTY_BPS
)
ALPHA_COST_BPS = float(os.getenv("V4_EXEC_ROUNDTRIP_BPS", str(DEFAULT_ROUNDTRIP_COST_BPS)))
ALPHA_MIN_TRAIN = int(os.getenv("ALPHA_MIN_TRAIN", "40"))
ALPHA_MIN_VALID = int(os.getenv("ALPHA_MIN_VALID", "18"))
ALPHA_MIN_NET_BPS = float(os.getenv("ALPHA_MIN_NET_BPS", "2.0"))
ALPHA_MAX_SPREAD_BPS = float(os.getenv("ALPHA_MAX_SPREAD_BPS", "8"))
PAPER_ALLOC_PCT = float(os.getenv("PAPER_ALLOC_PCT", "25")) / 100.0
MAX_ROWS = int(os.getenv("ALPHA_MAX_ROWS", "150000"))
VALIDATED_HORIZONS = tuple(
    int(x.strip()) for x in os.getenv("VALIDATED_HORIZONS", "60,120,300,600").split(",") if x.strip()
)

# Shadow paper is deliberately isolated from the validated PAPER ledger and LIVE gate.
# It records weaker, explicitly unvalidated state candidates to build evidence faster.
SHADOW_ENABLED = os.getenv("SHADOW_PAPER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
SHADOW_MIN_TRAIN = int(os.getenv("SHADOW_MIN_TRAIN", "8"))
SHADOW_MIN_VALID = int(os.getenv("SHADOW_MIN_VALID", "4"))
SHADOW_MIN_GROSS_BPS = float(os.getenv("SHADOW_MIN_GROSS_BPS", "0.25"))
SHADOW_MIN_HIT = float(os.getenv("SHADOW_MIN_HIT", "0.48"))
SHADOW_MIN_NET_BPS = float(os.getenv("SHADOW_MIN_NET_BPS", "0.0"))
SHADOW_MAX_OPEN = int(os.getenv("SHADOW_MAX_OPEN", "8"))
SHADOW_ALLOC_PCT = float(os.getenv("SHADOW_ALLOC_PCT", "12.5")) / 100.0
SHADOW_MAX_DD_PCT = float(os.getenv("SHADOW_MAX_DRAWDOWN_PCT", "35"))
SHADOW_HORIZONS = tuple(
    int(x.strip()) for x in os.getenv("SHADOW_HORIZONS", "30,60,120,300,600,900").split(",") if x.strip()
)

# Economic sensitivity lanes are diagnostics only. They answer whether the
# same observed gross edge would survive different fee/execution structures.
# Only the active execution mode can open SHADOW/PAPER trades.
ECONOMIC_COST_LANES_BPS = {
    "spot_market_taker": ALPHA_COST_BPS,
    "spot_post_only_proxy": float(os.getenv(
        "V4_SPOT_POST_ONLY_PROXY_BPS",
        str(2.0 * KRAKEN_MAKER_FEE_BPS + 2.0 * SLIPPAGE_BPS + EXECUTION_PENALTY_BPS),
    )),
    "futures_taker_proxy": float(os.getenv("V4_FUTURES_TAKER_PROXY_BPS", "17")),
    "futures_maker_proxy": float(os.getenv("V4_FUTURES_MAKER_PROXY_BPS", "11")),
}

# Preferred economic research path: maker-only perpetual futures. This is an
# isolated PAPER proxy driven by the spot microstructure signal and never
# counts toward LIVE readiness. Actual futures orderbook/fill validation is
# required before any promotion to real execution.
SCENARIO_ENABLED = os.getenv("SCENARIO_PAPER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
SCENARIO_NAME = "FUTURES_MAKER_PROXY"
SCENARIO_COST_BPS = ECONOMIC_COST_LANES_BPS["futures_maker_proxy"]
SCENARIO_MIN_NET_EDGE_BPS = float(os.getenv("SCENARIO_MIN_NET_EDGE_BPS", "5"))
SCENARIO_MIN_TRAIN = int(os.getenv("SCENARIO_MIN_TRAIN", "20"))
SCENARIO_MIN_VALID = int(os.getenv("SCENARIO_MIN_VALID", "12"))
SCENARIO_MIN_HIT = float(os.getenv("SCENARIO_MIN_HIT", "0.52"))
SCENARIO_MAX_OPEN = int(os.getenv("SCENARIO_MAX_OPEN", "4"))
SCENARIO_ALLOC_PCT = float(os.getenv("SCENARIO_ALLOC_PCT", "10")) / 100.0
SCENARIO_MAX_DD_PCT = float(os.getenv("SCENARIO_MAX_DRAWDOWN_PCT", "15"))
SCENARIO_HORIZONS = tuple(
    int(x.strip()) for x in os.getenv("SCENARIO_HORIZONS", "300,600,900").split(",") if x.strip()
)


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
    relative = ternary(float(get("relative_ret5_bps", 0.0)), 1.50)
    f = float(get("flow10", 0.0))
    r = float(get("ret5_bps", 0.0))
    # Pressure without movement: aggressive flow that price has not yet followed.
    absorption = ternary(f, 0.35) if abs(r) < 1.25 else 0
    vol = 1 if float(get("vol10_bps", 0.0)) > 3.0 else 0
    return f"o{obi}|f{flow}|m{pressure}|r{ret}|b{btc}|x{relative}|a{absorption}|v{vol}"


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
    # Purge the label horizon before validation so no training label can look
    # into the validation period.
    embargo_ms = int(horizon_s * 1000)
    train = x[x.ts_ms <= cutoff - embargo_ms]
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
        # Avoid a model being approved only because of a few large outliers.
        if tm["net_median_bps"] <= 0 or vm["net_median_bps"] <= 0:
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


def discover_shadow_models(raw: pd.DataFrame) -> list[dict[str, Any]]:
    """Find weaker state candidates for isolated SHADOW PAPER only.

    These candidates are intentionally NOT eligible for the validated PAPER ledger
    or LIVE bridge. Selection uses gross directional persistence with smaller
    sample requirements so the system can collect empirical trade outcomes faster.
    """
    if raw.empty:
        return []
    base = enrich_states(raw)
    all_models: list[dict[str, Any]] = []
    for horizon_s in SHADOW_HORIZONS:
        x = add_forward_label(base, horizon_s)
        x = x[x["spread_bps"].astype(float) <= ALPHA_MAX_SPREAD_BPS].copy()
        if len(x) < SHADOW_MIN_TRAIN + SHADOW_MIN_VALID:
            continue
        cutoff = int(x["ts_ms"].quantile(0.70))
        embargo_ms = int(horizon_s * 1000)
        train = x[x.ts_ms <= cutoff - embargo_ms]
        valid = x[x.ts_ms > cutoff]
        for (symbol, state), tg in train.groupby(["symbol", "state_key"]):
            if len(tg) < SHADOW_MIN_TRAIN:
                continue
            train_vals = tg["fwd_bps"].to_numpy(dtype=float)
            train_mean = float(np.mean(train_vals))
            if train_mean == 0:
                continue
            side = "LONG" if train_mean > 0 else "SHORT"
            sign = 1.0 if side == "LONG" else -1.0
            vg = valid[(valid.symbol == symbol) & (valid.state_key == state)]
            if len(vg) < SHADOW_MIN_VALID:
                continue
            valid_vals = vg["fwd_bps"].to_numpy(dtype=float)
            train_signed = sign * train_vals
            valid_signed = sign * valid_vals
            train_gross = float(np.mean(train_signed))
            valid_gross = float(np.mean(valid_signed))
            if train_gross < SHADOW_MIN_GROSS_BPS or valid_gross < SHADOW_MIN_GROSS_BPS:
                continue
            train_hit = float(np.mean(train_signed > 0))
            valid_hit = float(np.mean(valid_signed > 0))
            if train_hit < SHADOW_MIN_HIT or valid_hit < SHADOW_MIN_HIT:
                continue
            n_eff = min(len(train_signed), len(valid_signed))
            noise = max(
                float(np.std(train_signed, ddof=1)) if len(train_signed) > 1 else 1.0,
                float(np.std(valid_signed, ddof=1)) if len(valid_signed) > 1 else 1.0,
                1.0,
            )
            robust_gross = min(train_gross, valid_gross)
            net_edge = robust_gross - ALPHA_COST_BPS
            cost_positive = net_edge >= SHADOW_MIN_NET_BPS
            score_base = net_edge if cost_positive else robust_gross
            score = score_base * math.sqrt(n_eff) / noise
            all_models.append({
                "symbol": symbol,
                "state_key": state,
                "side": side,
                "horizon_s": int(horizon_s),
                "score": round(float(score), 5),
                "gross_edge_bps": round(float(robust_gross), 4),
                "net_edge_proxy_bps": round(float(net_edge), 4),
                "cost_positive": bool(cost_positive),
                "train_n": int(len(train_signed)),
                "valid_n": int(len(valid_signed)),
                "train_hit": round(train_hit, 5),
                "valid_hit": round(valid_hit, 5),
                "validation_level": "COST_POSITIVE_SHADOW" if cost_positive else "UNVALIDATED_SHADOW",
            })
    all_models.sort(
        key=lambda z: (bool(z.get("cost_positive")), z["score"], z["gross_edge_bps"]),
        reverse=True,
    )
    return all_models[:200]


def economic_sensitivity(shadow_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Diagnostic cost ladder; never counts as validation or opens trades."""
    lanes: dict[str, Any] = {}
    for name, cost_bps in ECONOMIC_COST_LANES_BPS.items():
        positives = [
            x for x in shadow_candidates
            if float(x.get("gross_edge_bps", 0.0)) - float(cost_bps) > 0
        ]
        best = max(
            (
                float(x.get("gross_edge_bps", 0.0)) - float(cost_bps)
                for x in shadow_candidates
            ),
            default=None,
        )
        lanes[name] = {
            "cost_bps": round(float(cost_bps), 4),
            "cost_positive_candidate_count": len(positives),
            "best_net_edge_proxy_bps": round(float(best), 4) if best is not None else None,
            "diagnostic_only": True,
        }
    return {
        "lanes": lanes,
        "note": "Futures lanes reuse spot signal gross edge only as an economic proxy; they are not futures validation.",
    }


def discover_models(db_path: Path = DB_PATH) -> dict[str, Any]:
    raw = load_snapshots(db_path)
    result = {
        "generated_ms": now_ms(),
        "rows": len(raw),
        "cost_bps": ALPHA_COST_BPS,
        "execution_mode": EXECUTION_MODE,
        "cost_model": {
            "maker_fee_bps_one_way": KRAKEN_MAKER_FEE_BPS,
            "taker_fee_bps_one_way": KRAKEN_TAKER_FEE_BPS,
            "slippage_bps_one_way": SLIPPAGE_BPS,
            "execution_penalty_bps_roundtrip": EXECUTION_PENALTY_BPS,
        },
        "horizons": {},
        "consensus": [],
    }
    for h in VALIDATED_HORIZONS:
        result["horizons"][str(h)] = discover_dataframe(raw, h)

    # Require agreement across two adjacent horizons. This opens the time
    # window without weakening the per-horizon validation requirements.
    consensus = []
    for h1, h2 in zip(VALIDATED_HORIZONS, VALIDATED_HORIZONS[1:]):
        left = {
            (m["symbol"], m["state_key"]): m
            for m in result["horizons"][str(h1)]["models"]
        }
        right = {
            (m["symbol"], m["state_key"]): m
            for m in result["horizons"][str(h2)]["models"]
        }
        for key, a in left.items():
            b = right.get(key)
            if not b or a["side"] != b["side"]:
                continue
            robust_edge = min(a["robust_edge_bps"], b["robust_edge_bps"])
            consensus.append({
                "symbol": a["symbol"],
                "state_key": a["state_key"],
                "side": a["side"],
                "score": round(min(a["score"], b["score"]), 5),
                "robust_edge_bps": robust_edge,
                "horizon_pair": [int(h1), int(h2)],
                "holding_horizon_s": int(h2),
                "n_valid_min": min(a["validation"]["n"], b["validation"]["n"]),
            })
    # Keep only the best horizon-pair for the same live state.
    best_by_state: dict[tuple[str, str], dict[str, Any]] = {}
    for item in consensus:
        key = (item["symbol"], item["state_key"])
        prev = best_by_state.get(key)
        if prev is None or (item["score"], item["robust_edge_bps"]) > (prev["score"], prev["robust_edge_bps"]):
            best_by_state[key] = item
    consensus = list(best_by_state.values())
    consensus.sort(key=lambda z: (z["score"], z["robust_edge_bps"]), reverse=True)
    result["validated_horizons"] = list(VALIDATED_HORIZONS)
    result["consensus"] = consensus[:50]
    result["shadow_candidates"] = discover_shadow_models(raw) if SHADOW_ENABLED else []
    result["economic_sensitivity"] = economic_sensitivity(result["shadow_candidates"])
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


def shadow_equity(db_path: Path = DB_PATH) -> float:
    value = get_meta("shadow_equity", START_CAPITAL, db_path)
    try:
        return float(value)
    except Exception:
        return START_CAPITAL


def scenario_equity(db_path: Path = DB_PATH) -> float:
    value = get_meta("scenario_equity", START_CAPITAL, db_path)
    try:
        return float(value)
    except Exception:
        return START_CAPITAL


def resolve_scenario_paper(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Resolve the futures-maker economic proxy on observed spot mids."""
    t = now_ms()
    closed: list[dict[str, Any]] = []
    with connect_db(db_path) as con:
        rows = con.execute(
            """SELECT id,opened_ms,symbol,side,horizon_s,entry,notional_czk,cost_bps
               FROM scenario_paper_trades
               WHERE scenario=? AND status='OPEN' AND opened_ms + horizon_s*1000 <= ?""",
            (SCENARIO_NAME, t),
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
                """UPDATE scenario_paper_trades
                   SET closed_ms=?,exit=?,pnl_czk=?,net_bps=?,status='CLOSED'
                   WHERE id=?""",
                (closed_ms, exit_px, pnl, net_bps, trade_id),
            )
            closed.append({
                "id": trade_id, "symbol": symbol, "side": side,
                "net_bps": round(net_bps, 3), "pnl_czk": round(pnl, 3),
            })
    if closed:
        eq = scenario_equity(db_path) + sum(x["pnl_czk"] for x in closed)
        set_meta("scenario_equity", eq, db_path)
    return closed


def maybe_open_scenario_paper(models: dict[str, Any], db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Open only economically viable futures-maker proxy candidates.

    This does not simulate futures basis/funding/queue fills and is therefore
    research-only. It cannot satisfy the validated PAPER/LIVE gate.
    """
    if not SCENARIO_ENABLED:
        return []
    candidates = []
    for model in models.get("shadow_candidates", []):
        gross = float(model.get("gross_edge_bps", 0.0))
        if int(model.get("horizon_s", 0)) not in SCENARIO_HORIZONS:
            continue
        if int(model.get("train_n", 0)) < SCENARIO_MIN_TRAIN or int(model.get("valid_n", 0)) < SCENARIO_MIN_VALID:
            continue
        if float(model.get("train_hit", 0.0)) < SCENARIO_MIN_HIT or float(model.get("valid_hit", 0.0)) < SCENARIO_MIN_HIT:
            continue
        scenario_net = gross - SCENARIO_COST_BPS
        if scenario_net < SCENARIO_MIN_NET_EDGE_BPS:
            continue
        item = dict(model)
        item["scenario_net_edge_bps"] = scenario_net
        candidates.append(item)
    if not candidates:
        return []

    eq = scenario_equity(db_path)
    if eq <= START_CAPITAL * (1.0 - SCENARIO_MAX_DD_PCT / 100.0):
        set_meta("scenario_paper_halted", {"reason": "MAX_DRAWDOWN", "equity": eq}, db_path)
        return []

    with connect_db(db_path) as con:
        open_rows = con.execute(
            "SELECT symbol FROM scenario_paper_trades WHERE scenario=? AND status='OPEN'",
            (SCENARIO_NAME,),
        ).fetchall()
    open_symbols = {r[0] for r in open_rows}
    slots = max(0, SCENARIO_MAX_OPEN - len(open_symbols))
    if slots <= 0:
        return []

    by_state: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for model in candidates:
        by_state.setdefault((model["symbol"], model["state_key"]), []).append(model)

    latest = latest_rows(db_path)
    t = now_ms()
    matches: list[tuple[float, dict[str, Any], Any]] = []
    for _, row in latest.iterrows():
        if row.symbol in open_symbols or t - int(row.ts_ms) > 5_000:
            continue
        if float(row.spread_bps) > ALPHA_MAX_SPREAD_BPS:
            continue
        options = by_state.get((row.symbol, row.state_key), [])
        if not options:
            continue
        model = max(options, key=lambda z: (z["scenario_net_edge_bps"], z["score"]))
        matches.append((model["scenario_net_edge_bps"], model, row))
    matches.sort(key=lambda z: (z[0], z[1]["score"]), reverse=True)

    notional = max(0.0, min(eq * SCENARIO_ALLOC_PCT, eq))
    opened: list[dict[str, Any]] = []
    for _, model, row in matches[:slots]:
        with connect_db(db_path) as con:
            cur = con.execute(
                """INSERT INTO scenario_paper_trades(
                    opened_ms,symbol,side,horizon_s,entry,notional_czk,
                    gross_edge_bps,score,cost_bps,state_key,scenario,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                (
                    t, row.symbol, model["side"], int(model["horizon_s"]), float(row.mid), notional,
                    float(model["gross_edge_bps"]), float(model["score"]), SCENARIO_COST_BPS,
                    row.state_key, SCENARIO_NAME,
                ),
            )
            trade_id = cur.lastrowid
        opened.append({
            "id": trade_id,
            "scenario": SCENARIO_NAME,
            "symbol": row.symbol,
            "side": model["side"],
            "horizon_s": int(model["horizon_s"]),
            "entry": float(row.mid),
            "notional_czk": round(notional, 2),
            "gross_edge_bps": float(model["gross_edge_bps"]),
            "scenario_net_edge_bps": round(float(model["scenario_net_edge_bps"]), 4),
            "cost_bps": SCENARIO_COST_BPS,
            "validation_level": "ECONOMIC_PROXY_ONLY",
        })
    return opened


def resolve_shadow_paper(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    t = now_ms()
    closed: list[dict[str, Any]] = []
    with connect_db(db_path) as con:
        rows = con.execute(
            """SELECT id,opened_ms,symbol,side,horizon_s,entry,notional_czk,cost_bps
               FROM shadow_paper_trades
               WHERE status='OPEN' AND opened_ms + horizon_s*1000 <= ?""",
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
                """UPDATE shadow_paper_trades
                   SET closed_ms=?,exit=?,pnl_czk=?,net_bps=?,status='CLOSED'
                   WHERE id=?""",
                (closed_ms, exit_px, pnl, net_bps, trade_id),
            )
            closed.append({
                "id": trade_id, "symbol": symbol, "side": side,
                "net_bps": round(net_bps, 3), "pnl_czk": round(pnl, 3),
            })
    if closed:
        eq = shadow_equity(db_path) + sum(x["pnl_czk"] for x in closed)
        set_meta("shadow_equity", eq, db_path)
    return closed


def maybe_open_shadow_paper(models: dict[str, Any], db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    if not SHADOW_ENABLED:
        return []
    candidates_raw = models.get("shadow_candidates", [])
    if not candidates_raw:
        return []

    eq = shadow_equity(db_path)
    if eq <= START_CAPITAL * (1.0 - SHADOW_MAX_DD_PCT / 100.0):
        set_meta("shadow_paper_halted", {"reason": "MAX_DRAWDOWN", "equity": eq}, db_path)
        return []

    with connect_db(db_path) as con:
        open_rows = con.execute(
            "SELECT symbol FROM shadow_paper_trades WHERE status='OPEN'"
        ).fetchall()
    open_symbols = {r[0] for r in open_rows}
    slots = max(0, SHADOW_MAX_OPEN - len(open_symbols))
    if slots <= 0:
        return []

    by_state: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for model in candidates_raw:
        by_state.setdefault((model["symbol"], model["state_key"]), []).append(model)

    latest = latest_rows(db_path)
    t = now_ms()
    matches: list[tuple[float, dict[str, Any], Any]] = []
    for _, row in latest.iterrows():
        if row.symbol in open_symbols or t - int(row.ts_ms) > 5_000:
            continue
        if float(row.spread_bps) > ALPHA_MAX_SPREAD_BPS:
            continue
        options = [
            z for z in by_state.get((row.symbol, row.state_key), [])
            if bool(z.get("cost_positive"))
        ]
        if not options:
            continue
        model = max(options, key=lambda z: (z["score"], z["net_edge_proxy_bps"]))
        matches.append((model["score"], model, row))
    matches.sort(key=lambda z: z[0], reverse=True)

    opened: list[dict[str, Any]] = []
    notional = max(0.0, min(eq * SHADOW_ALLOC_PCT, eq))
    for _, model, row in matches[:slots]:
        with connect_db(db_path) as con:
            cur = con.execute(
                """INSERT INTO shadow_paper_trades(
                    opened_ms,symbol,side,horizon_s,entry,notional_czk,
                    signal_edge_bps,signal_score,cost_bps,state_key,signal_kind,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                (
                    t, row.symbol, model["side"], model["horizon_s"], float(row.mid), notional,
                    model["gross_edge_bps"], model["score"], ALPHA_COST_BPS,
                    row.state_key, "COST_POSITIVE_SHADOW",
                ),
            )
            trade_id = cur.lastrowid
        opened.append({
            "id": trade_id, "symbol": row.symbol, "side": model["side"],
            "horizon_s": model["horizon_s"], "entry": float(row.mid),
            "notional_czk": round(notional, 2),
            "gross_edge_bps": model["gross_edge_bps"],
            "net_edge_proxy_bps": model["net_edge_proxy_bps"],
            "score": model["score"], "state_key": row.state_key,
            "validation_level": model["validation_level"],
        })
    return opened


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
                t, row.symbol, model["side"], int(model["holding_horizon_s"]), float(row.mid), notional,
                model["robust_edge_bps"],
                model["score"], ALPHA_COST_BPS, row.state_key,
            ),
        )
        trade_id = cur.lastrowid
    return {
        "id": trade_id, "symbol": row.symbol, "side": model["side"],
        "entry": float(row.mid), "notional_czk": round(notional, 2),
        "horizon_s": int(model["holding_horizon_s"]),
        "edge_bps": model["robust_edge_bps"],
        "horizon_pair": model["horizon_pair"],
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
                shadow_closed = resolve_shadow_paper(self.db_path)
                scenario_closed = resolve_scenario_paper(self.db_path)
                models = discover_models(self.db_path)
                opened = maybe_open_paper(models, self.db_path)
                shadow_opened = maybe_open_shadow_paper(models, self.db_path)
                scenario_opened = maybe_open_scenario_paper(models, self.db_path)
                self.last_models = models
                self.last_cycle_ms = now_ms()
                set_meta("alpha_last", {
                    "generated_ms": self.last_cycle_ms,
                    "rows": models["rows"],
                    "consensus_count": len(models["consensus"]),
                    "shadow_candidate_count": len(models.get("shadow_candidates", [])),
                    "opened": opened,
                    "closed": closed,
                    "shadow_opened": shadow_opened,
                    "shadow_closed": shadow_closed,
                    "scenario_opened": scenario_opened,
                    "scenario_closed": scenario_closed,
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
            shadow_open_n = con.execute("SELECT COUNT(*) FROM shadow_paper_trades WHERE status='OPEN'").fetchone()[0]
            shadow_closed_n = con.execute("SELECT COUNT(*) FROM shadow_paper_trades WHERE status='CLOSED'").fetchone()[0]
            scenario_open_n = con.execute(
                "SELECT COUNT(*) FROM scenario_paper_trades WHERE scenario=? AND status='OPEN'",
                (SCENARIO_NAME,),
            ).fetchone()[0]
            scenario_closed_n = con.execute(
                "SELECT COUNT(*) FROM scenario_paper_trades WHERE scenario=? AND status='CLOSED'",
                (SCENARIO_NAME,),
            ).fetchone()[0]
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
            recent_shadow = [
                {
                    "id": r[0], "symbol": r[1], "side": r[2], "horizon_s": r[3],
                    "status": r[4], "pnl_czk": r[5], "net_bps": r[6],
                    "signal_kind": r[7],
                }
                for r in con.execute(
                    """SELECT id,symbol,side,horizon_s,status,pnl_czk,net_bps,signal_kind
                       FROM shadow_paper_trades ORDER BY id DESC LIMIT 10"""
                ).fetchall()
            ]
            recent_scenario = [
                {
                    "id": r[0], "symbol": r[1], "side": r[2], "horizon_s": r[3],
                    "status": r[4], "pnl_czk": r[5], "net_bps": r[6],
                    "gross_edge_bps": r[7], "cost_bps": r[8],
                }
                for r in con.execute(
                    """SELECT id,symbol,side,horizon_s,status,pnl_czk,net_bps,gross_edge_bps,cost_bps
                       FROM scenario_paper_trades
                       WHERE scenario=? ORDER BY id DESC LIMIT 10""",
                    (SCENARIO_NAME,),
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
            "shadow": {
                "enabled": SHADOW_ENABLED,
                "equity": round(shadow_equity(self.db_path), 2),
                "open_trades": shadow_open_n,
                "closed_trades": shadow_closed_n,
                "candidate_count": len(self.last_models.get("shadow_candidates", [])) if self.last_models else 0,
                "cost_positive_candidate_count": (
                    sum(1 for x in self.last_models.get("shadow_candidates", []) if x.get("cost_positive"))
                    if self.last_models else 0
                ),
                "recent": recent_shadow,
                "counts_for_live_gate": False,
            },
            "preferred_scenario": {
                "enabled": SCENARIO_ENABLED,
                "name": SCENARIO_NAME,
                "cost_bps": SCENARIO_COST_BPS,
                "min_net_edge_bps": SCENARIO_MIN_NET_EDGE_BPS,
                "horizons": list(SCENARIO_HORIZONS),
                "equity": round(scenario_equity(self.db_path), 2),
                "open_trades": scenario_open_n,
                "closed_trades": scenario_closed_n,
                "recent": recent_scenario,
                "counts_for_live_gate": False,
                "actual_futures_validation_required": True,
                "live_orders": False,
            },
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
