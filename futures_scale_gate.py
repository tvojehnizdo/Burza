from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LOG_PATH = Path("data/futures_autopilot_events.jsonl")
AUDITED_PATH = Path("data/futures_scale_audited.jsonl")

MIN_STAGE1_TRADES = 20
MIN_STAGE2_TRADES = 40
MIN_STAGE3_TRADES = 80

STAGE1_PF = 1.25
STAGE2_PF = 1.40
STAGE3_PF = 1.60

STAGE2_MEAN_NET_BPS = 8.0
STAGE3_MEAN_NET_BPS = 12.0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def append_audited_result(row: dict[str, Any], path: Path = AUDITED_PATH) -> bool:
    trade_key = str(row.get("trade_key") or "")
    if not trade_key:
        return False
    existing = {
        str(x.get("trade_key") or "")
        for x in _read_jsonl(path)
        if str(x.get("event") or "") == "SETUP_V3_AUDITED_RESULT"
    }
    if trade_key in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"event": "SETUP_V3_AUDITED_RESULT", **row}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return True


def _rows(path: Path = LOG_PATH) -> list[dict[str, Any]]:
    audited = [
        x for x in _read_jsonl(AUDITED_PATH)
        if str(x.get("event") or "") == "SETUP_V3_AUDITED_RESULT"
    ]
    if audited:
        dedup: dict[str, dict[str, Any]] = {}
        for row in audited:
            key = str(row.get("trade_key") or "")
            if key:
                dedup[key] = row
        return list(dedup.values())

    # Fallback while no post-session audit has been persisted yet.
    return [
        x for x in _read_jsonl(path)
        if str(x.get("event") or "") == "SETUP_V3_TRADE_RESULT"
    ]


def evidence(path: Path = LOG_PATH) -> dict[str, Any]:
    rows = _rows(path)
    vals = [
        float(x.get("net_bps") if x.get("net_bps") is not None else x.get("approx_net_bps") or 0.0)
        for x in rows
    ]
    wins = [x for x in vals if x > 0]
    losses = [x for x in vals if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    total = sum(vals)
    mean = total / len(vals) if vals else 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in vals:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    multiplier = 1.0
    tier = "BASE"
    if len(vals) >= MIN_STAGE1_TRADES and total > 0 and pf >= STAGE1_PF:
        multiplier = 1.25
        tier = "EVIDENCE_1"
    if (
        len(vals) >= MIN_STAGE2_TRADES
        and total > 0
        and pf >= STAGE2_PF
        and mean >= STAGE2_MEAN_NET_BPS
    ):
        multiplier = 1.50
        tier = "EVIDENCE_2"
    if (
        len(vals) >= MIN_STAGE3_TRADES
        and total > 0
        and pf >= STAGE3_PF
        and mean >= STAGE3_MEAN_NET_BPS
    ):
        multiplier = 2.00
        tier = "EVIDENCE_3"

    return {
        "closed_setup_v3_trades": len(vals),
        "wins": len(wins),
        "losses": len(losses),
        "net_bps": round(total, 3),
        "mean_net_bps": round(mean, 3),
        "profit_factor": round(pf, 4),
        "max_cumulative_drawdown_bps": round(max_dd, 3),
        "scale_tier": tier,
        "scale_multiplier": multiplier,
        "evidence_source": "audited_exchange_history" if AUDITED_PATH.exists() else "manager_estimate",
        "requirements": {
            "stage1": {"trades": MIN_STAGE1_TRADES, "profit_factor": STAGE1_PF, "net_positive": True},
            "stage2": {"trades": MIN_STAGE2_TRADES, "profit_factor": STAGE2_PF, "mean_net_bps": STAGE2_MEAN_NET_BPS},
            "stage3": {"trades": MIN_STAGE3_TRADES, "profit_factor": STAGE3_PF, "mean_net_bps": STAGE3_MEAN_NET_BPS},
        },
    }


def scale_multiplier(path: Path = LOG_PATH) -> float:
    return float(evidence(path)["scale_multiplier"])


def selftest() -> dict[str, Any]:
    return {
        "ok": (
            MIN_STAGE1_TRADES < MIN_STAGE2_TRADES < MIN_STAGE3_TRADES
            and 1.0 < STAGE1_PF <= STAGE2_PF <= STAGE3_PF
        )
    }


if __name__ == "__main__":
    print(json.dumps(evidence(), indent=2, ensure_ascii=False))
