from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

from futures_canary import INVERT_DIRECTION, ROUND_TRIP_TAKER_COST_BPS, execution_signal_side

STATE_PATH = Path("data/futures_shadow_learning_state.json")
EVENT_LOG = Path("data/futures_shadow_learning_events.jsonl")
REPORT_PATH = Path("reports/futures_shadow_learning_latest.json")
TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"

HORIZONS_SEC = (60, 180, 300, 600)
OBSERVE_TOP_N = 10
DEDUPE_SEC = 60
MAX_OBSERVATIONS = 5000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _load() -> dict[str, Any]:
    default = {"version": 1, "observations": [], "last_seen": {}}
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            default.update(data)
    except Exception:
        pass
    if not isinstance(default.get("observations"), list):
        default["observations"] = []
    if not isinstance(default.get("last_seen"), dict):
        default["last_seen"] = {}
    return default


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["observations"] = list(state.get("observations") or [])[-MAX_OBSERVATIONS:]
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _event(kind: str, payload: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": _now_ms(), "event": kind, **payload}
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _ticker_mids() -> dict[str, float]:
    r = requests.get(TICKERS_URL, timeout=15)
    r.raise_for_status()
    rows = r.json().get("tickers") or []
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            if bid > 0 and ask >= bid:
                out[symbol] = (bid + ask) / 2.0
                continue
        except Exception:
            pass
        for key in ("markPrice", "last", "indexPrice"):
            try:
                px = float(row.get(key) or 0.0)
                if px > 0:
                    out[symbol] = px
                    break
            except Exception:
                continue
    return out


def _candidate_enrichment(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in [plan.get("candidate"), *(plan.get("alternatives") or [])]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            out[symbol] = item
    for item in plan.get("rejected_candidates") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            out.setdefault(symbol, item)
    return out


def observe_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Store candidate opportunities, including non-executed signals, for forward evaluation."""
    state = _load()
    now_ms = _now_ms()
    scan = plan.get("public_scan") or {}
    rows = [x for x in (scan.get("all") or []) if isinstance(x, dict)]
    rows.sort(
        key=lambda x: (
            bool(x.get("canary_signal_ready")),
            float(x.get("ranking_edge_bps") or x.get("taker_net_edge_bps") or -999.0),
            float(x.get("confidence") or 0.0),
        ),
        reverse=True,
    )
    enrich = _candidate_enrichment(plan)
    added = 0

    for row in rows[:OBSERVE_TOP_N]:
        symbol = str(row.get("symbol") or "").upper()
        base_side = str(row.get("side") or "").upper()
        execution_side = execution_signal_side(base_side)
        price = _num(row.get("price"), 0.0)
        if not symbol or base_side not in {"LONG", "SHORT"} or price <= 0:
            continue

        key = f"{symbol}|{base_side}|{'INV' if INVERT_DIRECTION else 'BASE'}"
        last = int((state.get("last_seen") or {}).get(key) or 0)
        if now_ms - last < DEDUPE_SEC * 1000:
            continue

        extra = enrich.get(symbol) or {}
        q = extra.get("quality") if isinstance(extra.get("quality"), dict) else extra.get("microstructure")
        if not isinstance(q, dict):
            q = {}

        obs = {
            "id": f"{now_ms}-{symbol}-{execution_side}",
            "observed_ts_ms": now_ms,
            "symbol": symbol,
            "side": execution_side,
            "base_side": base_side,
            "execution_side": execution_side,
            "direction_inverted": INVERT_DIRECTION,
            "entry_mid": price,
            "signal_ready": bool(row.get("canary_signal_ready")),
            "confidence": _num(row.get("confidence"), 0.0),
            "edge_bps": _num(row.get("taker_net_edge_bps"), 0.0),
            "ranking_edge_bps": _num(row.get("ranking_edge_bps"), _num(row.get("taker_net_edge_bps"), 0.0)),
            "volume_ratio": _num(row.get("volume_ratio"), 0.0),
            "regime": str(row.get("regime") or "UNKNOWN"),
            "market_regime": str(row.get("market_regime") or "UNKNOWN"),
            "breadth_alignment": int(row.get("breadth_alignment") or 0),
            "breakout": bool(row.get("breakout")),
            "quality_score": _num(extra.get("quality_score"), 0.0),
            "quality_tier": str(extra.get("quality_tier") or "UNRATED"),
            "micro_aligned_flow": _num(q.get("aligned_flow"), 0.0),
            "executed_candidate": bool(
                isinstance(plan.get("candidate"), dict)
                and str((plan.get("candidate") or {}).get("symbol") or "").upper() == symbol
            ),
            "horizons": {},
        }
        state["observations"].append(obs)
        state["last_seen"][key] = now_ms
        added += 1
        _event("SHADOW_OBSERVATION", obs)

    _save(state)
    return {"ok": True, "added": added, "total": len(state["observations"])}


def _directional_bps(side: str, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    direction = 1.0 if str(side).upper() == "LONG" else -1.0
    return direction * (current / entry - 1.0) * 10000.0


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "mean_net_bps": None, "hit_rate": None}
    vals = [_num(x.get("net_bps"), 0.0) for x in rows]
    return {
        "n": len(vals),
        "mean_net_bps": round(sum(vals) / len(vals), 3),
        "hit_rate": round(sum(1 for x in vals if x > 0) / len(vals), 4),
    }


def build_report(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or _load()
    resolved: list[dict[str, Any]] = []
    for obs in state.get("observations") or []:
        for horizon_key, result in (obs.get("horizons") or {}).items():
            if not isinstance(result, dict):
                continue
            resolved.append({
                "symbol": obs.get("symbol"),
                "side": obs.get("side"),
                "base_side": obs.get("base_side"),
                "execution_side": obs.get("execution_side"),
                "direction_inverted": obs.get("direction_inverted"),
                "signal_ready": obs.get("signal_ready"),
                "regime": obs.get("regime"),
                "market_regime": obs.get("market_regime"),
                "quality_tier": obs.get("quality_tier"),
                "breakout": obs.get("breakout"),
                "horizon_sec": int(horizon_key),
                "gross_bps": result.get("gross_bps"),
                "net_bps": result.get("net_bps"),
                "base_gross_bps": result.get("base_gross_bps"),
                "base_net_bps": result.get("base_net_bps"),
            })

    groups: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS_SEC:
        hr = [x for x in resolved if int(x["horizon_sec"]) == horizon]
        groups[f"h{horizon}_all"] = _bucket(hr)
        for field in ("quality_tier", "regime", "market_regime", "side", "direction_inverted"):
            values = sorted({str(x.get(field) or "UNKNOWN") for x in hr})
            for value in values:
                groups[f"h{horizon}_{field}_{value}"] = _bucket(
                    [x for x in hr if str(x.get(field) or "UNKNOWN") == value]
                )

    report = {
        "generated_ts_ms": _now_ms(),
        "observation_count": len(state.get("observations") or []),
        "resolved_rows": len(resolved),
        "modeled_round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "horizons_sec": list(HORIZONS_SEC),
        "groups": groups,
        "inverse_mode_active": INVERT_DIRECTION,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def resolve_due() -> dict[str, Any]:
    state = _load()
    observations = state.get("observations") or []
    if not observations:
        report = build_report(state)
        return {"ok": True, "resolved": 0, "report": report}

    now_ms = _now_ms()
    due = False
    for obs in observations:
        age_sec = (now_ms - int(obs.get("observed_ts_ms") or now_ms)) / 1000.0
        done = obs.get("horizons") or {}
        if any(age_sec >= h and str(h) not in done for h in HORIZONS_SEC):
            due = True
            break
    if not due:
        return {"ok": True, "resolved": 0, "report": None}

    try:
        mids = _ticker_mids()
    except Exception as exc:
        return {"ok": False, "resolved": 0, "error": f"{type(exc).__name__}: {exc}"}

    resolved_count = 0
    for obs in observations:
        symbol = str(obs.get("symbol") or "").upper()
        current = _num(mids.get(symbol), 0.0)
        if current <= 0:
            continue
        age_sec = (now_ms - int(obs.get("observed_ts_ms") or now_ms)) / 1000.0
        horizons = obs.setdefault("horizons", {})
        for horizon in HORIZONS_SEC:
            key = str(horizon)
            if age_sec < horizon or key in horizons:
                continue
            gross = _directional_bps(str(obs.get("execution_side") or obs.get("side") or ""), _num(obs.get("entry_mid"), 0.0), current)
            base_gross = _directional_bps(str(obs.get("base_side") or obs.get("side") or ""), _num(obs.get("entry_mid"), 0.0), current)
            result = {
                "resolved_ts_ms": now_ms,
                "exit_mid": current,
                "gross_bps": round(gross, 3),
                "net_bps": round(gross - ROUND_TRIP_TAKER_COST_BPS, 3),
                "base_gross_bps": round(base_gross, 3),
                "base_net_bps": round(base_gross - ROUND_TRIP_TAKER_COST_BPS, 3),
            }
            horizons[key] = result
            resolved_count += 1
            _event("SHADOW_RESOLVED", {
                "id": obs.get("id"),
                "symbol": symbol,
                "side": obs.get("side"),
                "base_side": obs.get("base_side"),
                "execution_side": obs.get("execution_side"),
                "direction_inverted": obs.get("direction_inverted"),
                "horizon_sec": horizon,
                **result,
            })

    _save(state)
    report = build_report(state)
    return {"ok": True, "resolved": resolved_count, "report": report}


def selftest() -> dict[str, Any]:
    long_gain = _directional_bps("LONG", 100.0, 101.0)
    short_gain = _directional_bps("SHORT", 100.0, 99.0)
    sample = [
        {"net_bps": 10.0},
        {"net_bps": -5.0},
        {"net_bps": 15.0},
    ]
    b = _bucket(sample)
    checks = {
        "long_direction": round(long_gain, 6) == 100.0,
        "short_direction": round(short_gain, 6) == 100.0,
        "bucket_count": b["n"] == 3,
        "bucket_hit_rate": abs(float(b["hit_rate"]) - (2.0 / 3.0)) < 1e-4,
        "horizons": HORIZONS_SEC == (60, 180, 300, 600),
        "inverse_mapping": (
            execution_signal_side("LONG") == ("SHORT" if INVERT_DIRECTION else "LONG")
            and execution_signal_side("SHORT") == ("LONG" if INVERT_DIRECTION else "SHORT")
        ),
    }
    return {"ok": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(selftest(), indent=2))
