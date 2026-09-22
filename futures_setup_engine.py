from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

CHARTS = "https://futures.kraken.com/api/charts/v1"

PROBE_LOOKBACK_MIN = 3
CONFIRM_LOOKBACK_MIN = 15
BREAKOUT_BUFFER_PCT = 0.0005
MIN_MOVE_FROM_ANCHOR_PCT = 0.0035
PROBE_EXPIRY_SEC = 20 * 60


@dataclass
class RangeSignal:
    symbol: str
    stage: str
    direction: str
    range_high: float
    range_low: float
    anchor_open: float
    close: float
    move_from_anchor_pct: float
    breakout_distance_bps: float
    lookback_min: int
    buffer_pct: float
    min_move_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _timestamp_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        x = float(value)
        if x > 1e12:
            return int(x)
        if x > 1e9:
            return int(x * 1000)
    except Exception:
        pass
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _candles(symbol: str, count: int = 80) -> pd.DataFrame:
    r = requests.get(
        f"{CHARTS}/trade/{str(symbol).upper()}/1m",
        params={"count": count},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json().get("candles") or []
    if not rows:
        raise RuntimeError(f"No 1m candles for {symbol}")
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < CONFIRM_LOOKBACK_MIN + 2:
        raise RuntimeError(f"Insufficient 1m candles for {symbol}: {len(df)}")
    return _completed_only(df)


def _completed_only(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time" not in df.columns:
        return df.copy()
    ts = _timestamp_ms(df["time"].iloc[-1])
    if ts is None:
        return df.copy()
    # Kraken chart timestamps are normally candle timestamps. If the last bar
    # started less than one minute ago, do not use it as a closed breakout bar.
    if int(time.time() * 1000) - ts < 60_000:
        return df.iloc[:-1].reset_index(drop=True)
    return df.copy()


def rolling_range_signal(
    df: pd.DataFrame,
    symbol: str,
    lookback_min: int,
    *,
    buffer_pct: float = BREAKOUT_BUFFER_PCT,
    min_move_pct: float = MIN_MOVE_FROM_ANCHOR_PCT,
    stage: str,
) -> RangeSignal | None:
    if len(df) < lookback_min + 1:
        return None

    prior = df.iloc[-(lookback_min + 1):-1]
    current = df.iloc[-1]
    if len(prior) != lookback_min:
        return None

    hi = float(prior["high"].max())
    lo = float(prior["low"].min())
    anchor_open = float(prior["open"].iloc[0])
    close = float(current["close"])
    if min(hi, lo, anchor_open, close) <= 0:
        return None

    move = close / anchor_open - 1.0
    upper = hi * (1.0 + buffer_pct)
    lower = lo * (1.0 - buffer_pct)

    if close > upper and move >= min_move_pct:
        dist = (close / hi - 1.0) * 10000.0
        return RangeSignal(
            symbol=str(symbol).upper(),
            stage=stage,
            direction="LONG",
            range_high=hi,
            range_low=lo,
            anchor_open=anchor_open,
            close=close,
            move_from_anchor_pct=move,
            breakout_distance_bps=dist,
            lookback_min=lookback_min,
            buffer_pct=buffer_pct,
            min_move_pct=min_move_pct,
        )

    if close < lower and move <= -min_move_pct:
        dist = (lo / close - 1.0) * 10000.0
        return RangeSignal(
            symbol=str(symbol).upper(),
            stage=stage,
            direction="SHORT",
            range_high=hi,
            range_low=lo,
            anchor_open=anchor_open,
            close=close,
            move_from_anchor_pct=move,
            breakout_distance_bps=dist,
            lookback_min=lookback_min,
            buffer_pct=buffer_pct,
            min_move_pct=min_move_pct,
        )
    return None


def snapshot(symbol: str) -> dict[str, Any]:
    df = _candles(symbol)
    probe = rolling_range_signal(
        df,
        symbol,
        PROBE_LOOKBACK_MIN,
        stage="PROBE",
    )
    confirmed = rolling_range_signal(
        df,
        symbol,
        CONFIRM_LOOKBACK_MIN,
        stage="CONFIRMED",
    )
    return {
        "symbol": str(symbol).upper(),
        "probe": probe.to_dict() if probe else None,
        "confirmed": confirmed.to_dict() if confirmed else None,
        "completed_bars": len(df),
    }


def fixed_range_reversal(
    symbol: str,
    original_direction: str,
    range_high: float,
    range_low: float,
    close: float,
    *,
    buffer_pct: float = BREAKOUT_BUFFER_PCT,
) -> dict[str, Any] | None:
    original = str(original_direction).upper()
    hi = float(range_high)
    lo = float(range_low)
    px = float(close)
    if min(hi, lo, px) <= 0 or original not in {"LONG", "SHORT"}:
        return None

    if original == "LONG" and px < lo * (1.0 - buffer_pct):
        return {
            "symbol": str(symbol).upper(),
            "direction": "SHORT",
            "close": px,
            "range_high": hi,
            "range_low": lo,
            "buffer_pct": buffer_pct,
            "breakout_distance_bps": round((lo / px - 1.0) * 10000.0, 3),
        }

    if original == "SHORT" and px > hi * (1.0 + buffer_pct):
        return {
            "symbol": str(symbol).upper(),
            "direction": "LONG",
            "close": px,
            "range_high": hi,
            "range_low": lo,
            "buffer_pct": buffer_pct,
            "breakout_distance_bps": round((px / hi - 1.0) * 10000.0, 3),
        }
    return None


def latest_completed_close(symbol: str) -> float:
    df = _candles(symbol)
    if df.empty:
        raise RuntimeError(f"No completed candles for {symbol}")
    return float(df["close"].iloc[-1])


def compatible_direction(candidate: dict[str, Any], expected: str) -> bool:
    source = candidate.get("source_signal") if isinstance(candidate.get("source_signal"), dict) else {}
    base = str(
        candidate.get("base_signal_side")
        or source.get("base_side")
        or source.get("side")
        or ""
    ).upper()
    return base == str(expected).upper()


def new_setup_from_signal(signal: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or time.time() * 1000)
    return {
        "symbol": str(signal["symbol"]).upper(),
        "stage": "PROBE_SHADOW" if str(signal["stage"]).upper() == "PROBE" else "CONFIRMED_READY",
        "original_direction": str(signal["direction"]).upper(),
        "range_high": float(signal["range_high"]),
        "range_low": float(signal["range_low"]),
        "anchor_open": float(signal["anchor_open"]),
        "signal_close": float(signal["close"]),
        "created_ts_ms": now_ms,
        "expires_ts_ms": now_ms + PROBE_EXPIRY_SEC * 1000,
        "first_live_opened": False,
        "first_live_closed": False,
        "reversal_used": False,
        "done": False,
    }


def selftest() -> dict[str, Any]:
    def bars(values: list[float]) -> pd.DataFrame:
        rows = []
        for i, close in enumerate(values):
            rows.append({
                "time": 1_700_000_000_000 + i * 60_000,
                "open": close,
                "high": close * 1.0005,
                "low": close * 0.9995,
                "close": close,
            })
        return pd.DataFrame(rows)

    up = [100.0] * 15 + [100.50]
    down = [100.0] * 15 + [99.50]
    sig_up = rolling_range_signal(bars(up), "PF_TESTUSD", 15, stage="CONFIRMED")
    sig_down = rolling_range_signal(bars(down), "PF_TESTUSD", 15, stage="CONFIRMED")
    rev = fixed_range_reversal("PF_TESTUSD", "LONG", 101.0, 99.0, 98.9)
    checks = {
        "confirmed_long": bool(sig_up and sig_up.direction == "LONG"),
        "confirmed_short": bool(sig_down and sig_down.direction == "SHORT"),
        "reversal_short": bool(rev and rev["direction"] == "SHORT"),
        "probe_expiry_positive": PROBE_EXPIRY_SEC > CONFIRM_LOOKBACK_MIN * 60,
    }
    return {"ok": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(selftest(), indent=2))
