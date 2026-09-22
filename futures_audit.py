from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

from futures_private import client_from_env, readiness
from futures_scale_gate import evidence as setup_v3_scale_evidence

REPORT_DIR = Path("reports")
AUTOPILOT_LOG = Path("data/futures_autopilot_events.jsonl")
CANARY_LOG = Path("data/futures_canary_events.jsonl")
STATE_PATH = Path("data/futures_autopilot_state.json")
CHARTS = "https://futures.kraken.com/api/charts/v1"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            continue
    return out


def _latest_session_start(events: list[dict[str, Any]], state: dict[str, Any]) -> int:
    starts = [
        int(e.get("ts_ms") or 0)
        for e in events
        if str(e.get("event") or "") in {
            "BOUNDED_SESSION_START",
            "LIVE_SESSION_START",
            "LIVE_MANAGER_START",
            "AUTOPILOT_START",
        }
        and int(e.get("ts_ms") or 0) > 0
    ]
    if starts:
        return max(starts)
    for key in ("session_start_ts_ms", "created_ts_ms"):
        try:
            value = int(state.get(key) or 0)
            if value > 0:
                return value
        except Exception:
            pass
    return int(time.time() * 1000) - 24 * 3600 * 1000


def _normalize_position_event(row: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "tradeable", "oldPosition", "newPosition", "positionChange",
        "executionPrice", "executionSize", "fee", "realizedPnL",
        "realizedFunding", "tradeType", "updateReason", "fillTime",
    }
    if expected.intersection(row.keys()):
        return row

    candidates: list[dict[str, Any]] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(obj, dict):
            if expected.intersection(obj.keys()):
                candidates.append(obj)
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, depth + 1)

    walk(row)

    if not candidates:
        return row

    best = max(candidates, key=lambda x: len(expected.intersection(x.keys())))
    merged = dict(row)
    merged.update(best)

    # Preserve wrapper timestamps/ids when payload omits them.
    if not merged.get("timestamp"):
        merged["timestamp"] = row.get("timestamp") or row.get("lastUpdateTimestamp")
    if not merged.get("fillTime"):
        merged["fillTime"] = best.get("fillTime") or row.get("fillTime")
    return merged


def _position_events(client: Any, since_ms: int) -> list[dict[str, Any]]:
    # Kraken history endpoint currently returns up to 100 events per request.
    body = client.position_events(since=since_ms, count=100, sort="asc")
    rows = body.get("elements") or []
    return [_normalize_position_event(x) for x in rows if isinstance(x, dict)]


def _candles(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            f"{CHARTS}/trade/{symbol}/1m",
            params={
                "from": max(0, start_ms // 1000 - 60),
                "to": end_ms // 1000 + 60,
            },
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json().get("candles") or []
        return [x for x in rows if isinstance(x, dict)]
    except Exception:
        return []


def _signal_events(canary: list[dict[str, Any]], since_ms: int) -> list[dict[str, Any]]:
    out = []
    for e in canary:
        ts = int(e.get("ts_ms") or 0)
        if ts < since_ms:
            continue
        if str(e.get("reason") or "") != "FUTURES_CANARY_LIVE_WITH_PROTECTION":
            continue
        cand = e.get("candidate") or {}
        if not isinstance(cand, dict):
            continue
        source = cand.get("source_signal") if isinstance(cand.get("source_signal"), dict) else {}
        micro = cand.get("microstructure") if isinstance(cand.get("microstructure"), dict) else {}
        out.append({
            "ts_ms": ts,
            "symbol": str(cand.get("symbol") or "").upper(),
            "side": str(cand.get("side") or "").lower(),
            "signal_mid": _num(cand.get("mid_price"), 0.0),
            "signal_edge_bps": _num(cand.get("taker_net_edge_bps"), 0.0),
            "confidence": _num(cand.get("confidence"), 0.0),
            "planned_stop": _num(cand.get("stop_price"), 0.0),
            "planned_take": _num(cand.get("take_profit_price"), 0.0),
            "quality_tier": str(cand.get("quality_tier") or "UNRATED"),
            "quality_score": _num(cand.get("quality_score"), 0.0),
            "regime": str(source.get("regime") or "UNKNOWN"),
            "market_regime": str(source.get("market_regime") or "UNKNOWN"),
            "breakout": bool(source.get("breakout")),
            "breadth_alignment": int(source.get("breadth_alignment") or 0),
            "micro_aligned_flow": _num(micro.get("aligned_flow"), 0.0),
            "base_signal_side": str(cand.get("base_signal_side") or source.get("base_side") or "").upper(),
            "execution_signal_side": str(cand.get("execution_signal_side") or source.get("execution_side") or "").upper(),
            "direction_inverted": bool(cand.get("direction_inverted") or source.get("direction_inverted")),
        })
    return out


def _nearest_signal(signals: list[dict[str, Any]], symbol: str, ts_ms: int) -> dict[str, Any] | None:
    eligible = [
        x for x in signals
        if x["symbol"] == symbol and abs(int(x["ts_ms"]) - ts_ms) <= 90_000
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda x: abs(int(x["ts_ms"]) - ts_ms))


def _trade_roundtrips(events: list[dict[str, Any]], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []

    for e in sorted(events, key=lambda x: int(x.get("timestamp") or x.get("fillTime") or 0)):
        symbol = str(e.get("tradeable") or "").upper()
        if not symbol:
            continue

        ts = int(e.get("fillTime") or e.get("timestamp") or 0)
        old_pos = _num(e.get("oldPosition"), 0.0)
        new_pos = _num(e.get("newPosition"), 0.0)
        change = str(e.get("positionChange") or "").lower()
        exec_px = _num(e.get("executionPrice"), 0.0)
        exec_size = abs(_num(e.get("executionSize"), 0.0))
        fee = _num(e.get("fee"), 0.0)
        pnl = _num(e.get("realizedPnL"), 0.0)
        funding = _num(e.get("realizedFunding"), 0.0)

        opened = old_pos == 0 and new_pos != 0
        closed = old_pos != 0 and new_pos == 0

        if opened or change == "open":
            direction = "long" if new_pos > 0 else "short"
            active[symbol] = {
                "symbol": symbol,
                "side": direction,
                "open_ts_ms": ts,
                "entry_price": exec_px,
                "entry_size": exec_size or abs(new_pos),
                "fees": fee,
                "realized_pnl": pnl,
                "funding": funding,
                "events": 1,
            }
            continue

        cur = active.get(symbol)
        if cur:
            cur["fees"] += fee
            cur["realized_pnl"] += pnl
            cur["funding"] += funding
            cur["events"] += 1

        if closed or change == "close":
            if not cur:
                cur = {
                    "symbol": symbol,
                    "side": "unknown",
                    "open_ts_ms": ts,
                    "entry_price": 0.0,
                    "entry_size": exec_size,
                    "fees": fee,
                    "realized_pnl": pnl,
                    "funding": funding,
                    "events": 1,
                }

            cur["close_ts_ms"] = ts
            cur["exit_price"] = exec_px
            cur["exit_size"] = exec_size
            cur["hold_sec"] = max(0.0, (ts - int(cur["open_ts_ms"])) / 1000.0)
            cur["net_after_fee"] = cur["realized_pnl"] + cur["funding"] - cur["fees"]

            sig = _nearest_signal(signals, symbol, int(cur["open_ts_ms"]))
            if sig:
                cur["signal_edge_bps"] = sig["signal_edge_bps"]
                cur["signal_confidence"] = sig["confidence"]
                cur["signal_mid"] = sig["signal_mid"]
                cur["planned_stop"] = sig["planned_stop"]
                cur["planned_take"] = sig["planned_take"]
                cur["quality_tier"] = sig.get("quality_tier")
                cur["quality_score"] = sig.get("quality_score")
                cur["regime"] = sig.get("regime")
                cur["market_regime"] = sig.get("market_regime")
                cur["breakout"] = sig.get("breakout")
                cur["breadth_alignment"] = sig.get("breadth_alignment")
                cur["micro_aligned_flow"] = sig.get("micro_aligned_flow")
                cur["base_signal_side"] = sig.get("base_signal_side")
                cur["execution_signal_side"] = sig.get("execution_signal_side")
                cur["direction_inverted"] = sig.get("direction_inverted")
                if sig["signal_mid"] > 0 and cur["entry_price"] > 0:
                    direction = 1.0 if cur["side"] == "long" else -1.0
                    cur["entry_slippage_bps"] = direction * (
                        cur["entry_price"] / sig["signal_mid"] - 1.0
                    ) * 10000.0

            candles = _candles(symbol, int(cur["open_ts_ms"]), ts)
            if candles and cur["entry_price"] > 0:
                highs = [_num(x.get("high"), math.nan) for x in candles]
                lows = [_num(x.get("low"), math.nan) for x in candles]
                highs = [x for x in highs if math.isfinite(x)]
                lows = [x for x in lows if math.isfinite(x)]
                if highs and lows:
                    entry = cur["entry_price"]
                    if cur["side"] == "long":
                        cur["mfe_bps"] = max(0.0, (max(highs) / entry - 1.0) * 10000.0)
                        cur["mae_bps"] = max(0.0, (1.0 - min(lows) / entry) * 10000.0)
                    elif cur["side"] == "short":
                        cur["mfe_bps"] = max(0.0, (1.0 - min(lows) / entry) * 10000.0)
                        cur["mae_bps"] = max(0.0, (max(highs) / entry - 1.0) * 10000.0)

            completed.append(cur)
            active.pop(symbol, None)

    return completed


def _event_diagnostics(events: list[dict[str, Any]]) -> dict[str, Any]:
    position_changes = Counter()
    update_reasons = Counter()
    trade_types = Counter()
    key_counts = Counter()
    samples: list[dict[str, Any]] = []

    for e in events:
        position_changes[str(e.get("positionChange") or "missing")] += 1
        update_reasons[str(e.get("updateReason") or "missing")] += 1
        trade_types[str(e.get("tradeType") or "missing")] += 1
        for key in e.keys():
            key_counts[str(key)] += 1

    for e in events[-12:]:
        samples.append({
            "timestamp": e.get("timestamp"),
            "fillTime": e.get("fillTime"),
            "tradeable": e.get("tradeable"),
            "oldPosition": e.get("oldPosition"),
            "newPosition": e.get("newPosition"),
            "positionChange": e.get("positionChange"),
            "updateReason": e.get("updateReason"),
            "executionPrice": e.get("executionPrice"),
            "executionSize": e.get("executionSize"),
            "fee": e.get("fee"),
            "realizedPnL": e.get("realizedPnL"),
            "realizedFunding": e.get("realizedFunding"),
            "tradeType": e.get("tradeType"),
        })

    return {
        "position_change_counts": dict(position_changes),
        "update_reason_counts": dict(update_reasons),
        "trade_type_counts": dict(trade_types),
        "raw_top_level_key_counts": dict(key_counts),
        "last_events": samples,
    }


def _exit_reason_map(events: list[dict[str, Any]], since_ms: int) -> tuple[Counter, list[dict[str, Any]]]:
    rows = []
    counts: Counter = Counter()
    for e in events:
        if int(e.get("ts_ms") or 0) < since_ms:
            continue
        if str(e.get("event") or "") != "AUTO_EXIT":
            continue
        reason = str(e.get("exit_reason") or e.get("reason") or "UNKNOWN")
        counts[reason] += 1
        rows.append({
            "ts_ms": e.get("ts_ms"),
            "symbol": e.get("symbol"),
            "exit_reason": reason,
            "result_reason": e.get("reason"),
            "ok": e.get("ok"),
            "pnl_bps_before_close": e.get("pnl_bps_before_close"),
            "close_submitted_live": ((e.get("close") or {}).get("submitted_live") if isinstance(e.get("close"), dict) else None),
        })
    return counts, rows


def _attach_exit_reasons(trades: list[dict[str, Any]], exit_rows: list[dict[str, Any]]) -> None:
    for trade in trades:
        symbol = trade["symbol"]
        close_ts = int(trade.get("close_ts_ms") or 0)
        matches = [
            e for e in exit_rows
            if str(e.get("symbol") or "").upper() == symbol
            and abs(int(e.get("ts_ms") or 0) - close_ts) <= 90_000
        ]
        if matches:
            best = min(matches, key=lambda e: abs(int(e.get("ts_ms") or 0) - close_ts))
            trade["exit_reason"] = str(best.get("exit_reason") or best.get("reason") or "UNKNOWN")


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "completed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "realized_pnl": 0.0,
            "fees": 0.0,
            "funding": 0.0,
            "net_after_fee": 0.0,
        }

    nets = [_num(t.get("net_after_fee")) for t in trades]
    holds = [_num(t.get("hold_sec")) for t in trades]
    mfes = [_num(t.get("mfe_bps")) for t in trades if t.get("mfe_bps") is not None]
    maes = [_num(t.get("mae_bps")) for t in trades if t.get("mae_bps") is not None]
    slips = [_num(t.get("entry_slippage_bps")) for t in trades if t.get("entry_slippage_bps") is not None]
    edges = [_num(t.get("signal_edge_bps")) for t in trades if t.get("signal_edge_bps") is not None]

    wins = sum(1 for x in nets if x > 0)
    losses = sum(1 for x in nets if x < 0)
    return {
        "completed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "flat": len(trades) - wins - losses,
        "win_rate_pct": round(wins / len(trades) * 100.0, 2),
        "realized_pnl": round(sum(_num(t.get("realized_pnl")) for t in trades), 8),
        "fees": round(sum(_num(t.get("fees")) for t in trades), 8),
        "funding": round(sum(_num(t.get("funding")) for t in trades), 8),
        "net_after_fee": round(sum(nets), 8),
        "avg_net_after_fee": round(statistics.mean(nets), 8),
        "median_net_after_fee": round(statistics.median(nets), 8),
        "avg_hold_sec": round(statistics.mean(holds), 1),
        "median_hold_sec": round(statistics.median(holds), 1),
        "avg_mfe_bps": round(statistics.mean(mfes), 2) if mfes else None,
        "avg_mae_bps": round(statistics.mean(maes), 2) if maes else None,
        "avg_entry_slippage_bps": round(statistics.mean(slips), 2) if slips else None,
        "avg_signal_edge_bps": round(statistics.mean(edges), 2) if edges else None,
    }



def _group_performance(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(key) or "UNKNOWN")].append(trade)

    out: dict[str, Any] = {}
    for name, rows in sorted(groups.items()):
        nets = [_num(x.get("net_after_fee")) for x in rows]
        wins = sum(1 for x in nets if x > 0)
        out[name] = {
            "trades": len(rows),
            "wins": wins,
            "win_rate_pct": round(wins / len(rows) * 100.0, 2) if rows else None,
            "net_after_fee": round(sum(nets), 8),
            "avg_net_after_fee": round(statistics.mean(nets), 8) if nets else None,
            "avg_mfe_bps": round(
                statistics.mean([_num(x.get("mfe_bps")) for x in rows if x.get("mfe_bps") is not None]),
                2,
            ) if any(x.get("mfe_bps") is not None for x in rows) else None,
            "avg_mae_bps": round(
                statistics.mean([_num(x.get("mae_bps")) for x in rows if x.get("mae_bps") is not None]),
                2,
            ) if any(x.get("mae_bps") is not None for x in rows) else None,
        }
    return out


def _recommendations(summary: dict[str, Any], trades: list[dict[str, Any]], exit_counts: Counter) -> list[str]:
    out: list[str] = []
    n = int(summary.get("completed_trades") or 0)
    if n == 0:
        return ["Zatim nejsou uzavrene obchody; strategicke parametry nemenit podle nuloveho vzorku."]

    avg_slip = summary.get("avg_entry_slippage_bps")
    if avg_slip is not None and avg_slip > 5:
        out.append("Vstupni slippage je vysoka; zvazit prisnejsi spread/liquidity filtr nebo limitni vstupy.")

    avg_mfe = summary.get("avg_mfe_bps")
    avg_mae = summary.get("avg_mae_bps")
    if avg_mfe is not None and avg_mae is not None:
        if avg_mfe < 30 and avg_mae > avg_mfe:
            out.append("Obchody maji maly favorable excursion proti adverse excursion; zprísnit vstupni edge/confirmation.")
        if avg_mfe > 45 and exit_counts.get("NO_PROGRESS", 0) > 0:
            out.append("Cast obchodu se dostane do zisku a pak skonci jako no-progress; zvazit drivejsi profit capture/trailing.")

    if exit_counts.get("HARD_MAX_HOLD", 0) > max(1, n // 3):
        out.append("Prilis mnoho hard-time exitu; zkratit signalovou platnost nebo zprísnit vstupni filtr.")

    if exit_counts.get("NO_PROGRESS", 0) > max(1, n // 2):
        out.append("No-progress dominuje; zkratit 3min timeout nebo zvysit minimalni momentum pred vstupem.")

    if summary.get("net_after_fee", 0.0) < 0 and summary.get("fees", 0.0) > 0:
        out.append("Po poplatcich je vzorek zaporny; nezvysovat velikost pozic, dokud se nepotvrdi kladny net edge.")

    if n < 30:
        out.append("Vzorek je stale maly; zmeny delat pouze u zjevnych technickych nebo nakladovych problemu.")
    else:
        out.append("Vzorek uz umoznuje A/B upravu jednoho parametru po druhem.")

    return out


def _md(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Futures audit",
        "",
        f"- Session start: {report['session_start_ms']}",
        f"- Equity start/current: {report['equity_start_usd']:.6f} / {report['equity_current_usd']:.6f} USD",
        f"- Equity delta: {report['equity_delta_usd']:.6f} USD",
        f"- Completed trades: {s['completed_trades']}",
        f"- Win rate: {s['win_rate_pct']}",
        f"- Realized PnL: {s['realized_pnl']}",
        f"- Fees: {s['fees']}",
        f"- Funding: {s['funding']}",
        f"- Net after fee: {s['net_after_fee']}",
        f"- Avg hold: {s.get('avg_hold_sec')} s",
        f"- Avg MFE/MAE: {s.get('avg_mfe_bps')} / {s.get('avg_mae_bps')} bps",
        f"- Avg entry slippage: {s.get('avg_entry_slippage_bps')} bps",
        "",
        "## Exit reasons",
    ]
    for k, v in report["exit_reasons"].items():
        lines.append(f"- {k}: {v}")

    lines += ["", "## Performance by signal class", ""]
    for group_name, group_rows in (report.get("performance_by") or {}).items():
        lines.append(f"### {group_name}")
        if not group_rows:
            lines.append("- no data")
            continue
        for name, stats in group_rows.items():
            lines.append(
                f"- {name}: n={stats.get('trades')}, win={stats.get('win_rate_pct')}%, "
                f"net={stats.get('net_after_fee')}, avg={stats.get('avg_net_after_fee')}, "
                f"MFE/MAE={stats.get('avg_mfe_bps')}/{stats.get('avg_mae_bps')} bps"
            )

    lines += ["", "## Trades", ""]
    if report["trades"]:
        lines.append("| Symbol | Side | Hold s | PnL | Fee | Net | MFE bps | MAE bps | Slip bps | Exit |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for t in report["trades"]:
            lines.append(
                f"| {t.get('symbol')} | {t.get('side')} | {round(_num(t.get('hold_sec')),1)} | "
                f"{round(_num(t.get('realized_pnl')),6)} | {round(_num(t.get('fees')),6)} | "
                f"{round(_num(t.get('net_after_fee')),6)} | {round(_num(t.get('mfe_bps')),2)} | "
                f"{round(_num(t.get('mae_bps')),2)} | {round(_num(t.get('entry_slippage_bps')),2)} | "
                f"{t.get('exit_reason','exchange/unknown')} |"
            )
    else:
        lines.append("No completed trades in current audit window.")

    lines += ["", "## Audit diagnostics", ""]
    if report.get("audit_anomalies"):
        for item in report["audit_anomalies"]:
            lines.append(f"- {item}")
    else:
        lines.append("- No audit reconstruction anomaly detected.")

    diag = report.get("exchange_event_diagnostics") or {}
    lines.append(f"- Kraken position events: {report.get('exchange_position_events')}")
    lines.append(f"- positionChange counts: {diag.get('position_change_counts')}")
    lines.append(f"- updateReason counts: {diag.get('update_reason_counts')}")
    lines.append(f"- tradeType counts: {diag.get('trade_type_counts')}")
    lines.append(f"- event key counts: {diag.get('raw_top_level_key_counts')}")
    lines.append(f"- Local AUTO_EXIT attempts: {len(report.get('local_exit_attempts') or [])}")

    for row in report.get("local_exit_attempts") or []:
        lines.append(
            f"  - {row.get('symbol')} | exit={row.get('exit_reason')} | "
            f"result={row.get('result_reason')} | ok={row.get('ok')} | "
            f"submitted={row.get('close_submitted_live')} | "
            f"pnl_bps={row.get('pnl_bps_before_close')}"
        )

    scale = report.get("setup_v3_scale_evidence") or {}
    lines += ["", "## Setup V3 scale evidence", ""]
    lines.append(
        f"- closed={scale.get('closed_setup_v3_trades')} | tier={scale.get('scale_tier')} | "
        f"multiplier={scale.get('scale_multiplier')} | PF={scale.get('profit_factor')} | "
        f"mean_net_bps={scale.get('mean_net_bps')} | net_bps={scale.get('net_bps')}"
    )

    lines += ["", "## Recommendations", ""]
    for r in report["recommendations"]:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    client = client_from_env()
    r = readiness()
    state = _load_json(STATE_PATH)
    auto_events = _load_jsonl(AUTOPILOT_LOG)
    canary_events = _load_jsonl(CANARY_LOG)

    since_ms = _latest_session_start(auto_events, state)
    history_since_ms = max(0, since_ms - 24 * 3600 * 1000)
    exchange_events = _position_events(client, history_since_ms)
    signals = _signal_events(canary_events, history_since_ms)
    all_trades = _trade_roundtrips(exchange_events, signals)
    trades = [
        t for t in all_trades
        if int(t.get("close_ts_ms") or 0) >= since_ms
    ]
    exit_counts, exit_rows = _exit_reason_map(auto_events, since_ms)
    _attach_exit_reasons(trades, exit_rows)

    summary = _summary(trades)
    exchange_diag = _event_diagnostics(exchange_events)
    anomalies: list[str] = []
    if sum(exit_counts.values()) > 0 and int(summary.get("completed_trades") or 0) == 0:
        anomalies.append(
            "Local AUTO_EXIT events exist but no completed Kraken round-trip was reconstructed."
        )
    if not exchange_events:
        anomalies.append(
            "Kraken position-event history returned zero events for the extended audit window."
        )

    current_equity = _num(r.get("equity_usd"))
    start_equity = _num(state.get("session_start_equity"), current_equity)

    report = {
        "generated_ts_ms": int(time.time() * 1000),
        "session_start_ms": since_ms,
        "equity_start_usd": start_equity,
        "equity_current_usd": current_equity,
        "equity_delta_usd": round(current_equity - start_equity, 8),
        "open_position_count": int(r.get("open_position_count") or 0),
        "open_positions": r.get("open_positions"),
        "open_orders": r.get("open_orders"),
        "history_since_ms": history_since_ms,
        "exchange_position_events": len(exchange_events),
        "exchange_first_event_ts_ms": min(
            [int(e.get("timestamp") or e.get("fillTime") or 0) for e in exchange_events if int(e.get("timestamp") or e.get("fillTime") or 0) > 0],
            default=None,
        ),
        "exchange_last_event_ts_ms": max(
            [int(e.get("timestamp") or e.get("fillTime") or 0) for e in exchange_events if int(e.get("timestamp") or e.get("fillTime") or 0) > 0],
            default=None,
        ),
        "canary_signal_events": len(signals),
        "exchange_event_diagnostics": exchange_diag,
        "local_exit_attempts": exit_rows,
        "summary": summary,
        "setup_v3_scale_evidence": setup_v3_scale_evidence(),
        "performance_by": {
            "quality_tier": _group_performance(trades, "quality_tier"),
            "regime": _group_performance(trades, "regime"),
            "market_regime": _group_performance(trades, "market_regime"),
            "side": _group_performance(trades, "side"),
            "breakout": _group_performance(trades, "breakout"),
            "direction_inverted": _group_performance(trades, "direction_inverted"),
            "base_signal_side": _group_performance(trades, "base_signal_side"),
            "execution_signal_side": _group_performance(trades, "execution_signal_side"),
        },
        "exit_reasons": dict(exit_counts),
        "trades": trades,
        "audit_anomalies": anomalies,
        "recommendations": _recommendations(summary, trades, exit_counts),
    }

    json_path = REPORT_DIR / "futures_audit_latest.json"
    md_path = REPORT_DIR / "futures_audit_latest.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md_path.write_text(_md(report), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "report_json": str(json_path),
        "report_md": str(md_path),
        "summary": summary,
        "exit_reasons": dict(exit_counts),
        "exchange_position_events": report["exchange_position_events"],
        "audit_anomalies": report["audit_anomalies"],
        "recommendations": report["recommendations"],
        "open_position_count": report["open_position_count"],
        "equity_start_usd": report["equity_start_usd"],
        "equity_current_usd": report["equity_current_usd"],
        "equity_delta_usd": report["equity_delta_usd"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
