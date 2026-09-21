from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from futures_private import (
    client_from_env,
    load_policy as load_futures_policy,
    place_order,
    position_map,
    readiness as futures_readiness,
    round_size_down,
    save_policy as save_futures_policy,
)
from relative_value import DB_PATH as RV_DB_PATH, init_db as init_rv_db, scan_opportunities

POLICY_PATH = Path(os.getenv("RV_LIVE_POLICY", "data/rv_live_policy.json"))
LOG_PATH = Path(os.getenv("RV_LIVE_LOG", "data/rv_live_events.jsonl"))

DEFAULT_POLICY = {
    "live_execution": False,
    "allow_new_entries": False,
    "target_notional_usd_per_leg": 10.0,
    "max_live_pairs": 1,
    "min_closed_paper_pairs": 20,
    "min_profit_factor": 1.20,
    "min_net_pnl_czk": 1.0,
    "max_paper_drawdown_pct": 5.0,
    "poll_s": 2.0,
    "require_clean_account_on_first_arm": True,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def load_policy() -> dict[str, Any]:
    p = dict(DEFAULT_POLICY)
    if POLICY_PATH.exists():
        try:
            p.update(json.loads(POLICY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    p["live_execution"] = bool(p.get("live_execution", False))
    p["allow_new_entries"] = bool(p.get("allow_new_entries", False))
    p["target_notional_usd_per_leg"] = min(max(float(p.get("target_notional_usd_per_leg", 10.0)), 1.0), 15.0)
    p["max_live_pairs"] = 1
    p["min_closed_paper_pairs"] = max(int(p.get("min_closed_paper_pairs", 20)), 20)
    p["min_profit_factor"] = max(float(p.get("min_profit_factor", 1.20)), 1.0)
    p["min_net_pnl_czk"] = max(float(p.get("min_net_pnl_czk", 1.0)), 0.0)
    p["max_paper_drawdown_pct"] = min(max(float(p.get("max_paper_drawdown_pct", 5.0)), 0.5), 10.0)
    p["poll_s"] = min(max(float(p.get("poll_s", 2.0)), 1.0), 10.0)
    p["require_clean_account_on_first_arm"] = True
    return p


def save_policy(patch: dict[str, Any]) -> dict[str, Any]:
    p = load_policy()
    for key, value in patch.items():
        if key in DEFAULT_POLICY:
            p[key] = value
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")
    p = load_policy()
    POLICY_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")
    return p


def init_live_db(path: Path = RV_DB_PATH) -> None:
    init_rv_db(path)
    with sqlite3.connect(path) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rv_live_pairs(
                paper_id INTEGER PRIMARY KEY,
                opened_ms INTEGER NOT NULL,
                closed_ms INTEGER,
                root TEXT NOT NULL,
                perp_symbol TEXT NOT NULL,
                fixed_symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                size_base REAL NOT NULL,
                target_notional_usd REAL NOT NULL,
                open_fixed_result TEXT,
                open_perp_result TEXT,
                close_fixed_result TEXT,
                close_perp_result TEXT,
                compensation_result TEXT,
                status TEXT NOT NULL,
                error TEXT
            )"""
        )


def _paper_rows(path: Path = RV_DB_PATH) -> list[tuple[Any, ...]]:
    init_live_db(path)
    with sqlite3.connect(path) as con:
        return con.execute(
            """SELECT id,opened_ms,closed_ms,root,perp_symbol,fixed_symbol,direction,
                      pnl_czk,net_pnl_bps,status
               FROM rv_paper_pairs ORDER BY id ASC"""
        ).fetchall()


def paper_evidence(path: Path = RV_DB_PATH) -> dict[str, Any]:
    rows = _paper_rows(path)
    closed = [r for r in rows if str(r[9]) == "CLOSED" and r[7] is not None]
    pnls = [float(r[7]) for r in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else (99.0 if wins else 0.0)

    equity = 5000.0
    peak = equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    p = load_policy()
    gate = (
        len(closed) >= int(p["min_closed_paper_pairs"])
        and sum(pnls) >= float(p["min_net_pnl_czk"])
        and pf >= float(p["min_profit_factor"])
        and max_dd * 100.0 <= float(p["max_paper_drawdown_pct"])
    )
    return {
        "database": str(path),
        "closed_pairs": len(closed),
        "net_pnl_czk": round(sum(pnls), 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "profit_factor": round(float(pf), 4),
        "max_drawdown_pct": round(max_dd * 100.0, 4),
        "gate": gate,
        "requirements": {
            "min_closed_paper_pairs": p["min_closed_paper_pairs"],
            "min_net_pnl_czk": p["min_net_pnl_czk"],
            "min_profit_factor": p["min_profit_factor"],
            "max_paper_drawdown_pct": p["max_paper_drawdown_pct"],
        },
    }


def _managed_open(path: Path = RV_DB_PATH) -> list[dict[str, Any]]:
    init_live_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT paper_id,root,perp_symbol,fixed_symbol,direction,size_base,status
               FROM rv_live_pairs WHERE status IN ('OPEN','CLOSE_ERROR','OPENING','OPEN_ERROR')
               ORDER BY opened_ms"""
        ).fetchall()
    return [
        {
            "paper_id": int(r[0]), "root": r[1], "perp_symbol": r[2],
            "fixed_symbol": r[3], "direction": r[4], "size_base": float(r[5]),
            "status": r[6],
        }
        for r in rows
    ]


def readiness(path: Path = RV_DB_PATH) -> dict[str, Any]:
    evidence = paper_evidence(path)
    policy = load_policy()
    try:
        fr = futures_readiness()
        futures_ok = bool(fr.get("safe_to_arm"))
        actual_positions = int(fr.get("open_position_count") or 0)
    except Exception as exc:
        fr = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        futures_ok = False
        actual_positions = -1

    managed = _managed_open(path)
    if managed:
        account_clean = actual_positions in {2, -1}
    else:
        account_clean = actual_positions == 0

    blockers: list[str] = []
    if not evidence["gate"]:
        blockers.append("paper_evidence_gate")
    if not futures_ok:
        blockers.append("futures_api_readiness")
    if policy.get("require_clean_account_on_first_arm") and not account_clean:
        blockers.append("unmanaged_or_nonclean_futures_positions")
    if managed and len(managed) > int(policy["max_live_pairs"]):
        blockers.append("managed_live_pair_limit")

    return {
        "ok": not blockers,
        "safe_to_arm": not blockers,
        "policy": policy,
        "paper_evidence": evidence,
        "futures": fr,
        "managed_open": managed,
        "account_clean_for_managed_state": account_clean,
        "blockers": blockers,
        "live_execution": bool(policy.get("live_execution")) and bool(load_futures_policy().get("live_execution")),
        "allow_new_entries": bool(policy.get("allow_new_entries")),
    }


def _event(kind: str, payload: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": _now_ms(), "kind": kind, **payload}
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def set_live_execution(enabled: bool, path: Path = RV_DB_PATH) -> dict[str, Any]:
    if enabled:
        r = readiness(path)
        if not r.get("safe_to_arm"):
            raise RuntimeError("RV live readiness failed: " + ",".join(r.get("blockers") or []))
        save_futures_policy({"live_execution": True})
        p = save_policy({"live_execution": True, "allow_new_entries": True})
        _event("ARM", {"paper_evidence": r["paper_evidence"]})
        return {"armed": True, "allow_new_entries": True, "policy": p, "readiness": r}

    managed = _managed_open(path)
    if managed:
        p = save_policy({"live_execution": True, "allow_new_entries": False})
        save_futures_policy({"live_execution": True})
        close_results: list[dict[str, Any]] = []
        for target in _all_managed_targets(path):
            try:
                close_results.append(close_live_pair(target, path))
            except Exception as exc:
                close_results.append({
                    "paper_id": target["paper_id"],
                    "status": "CLOSE_ERROR",
                    "errors": [f"{type(exc).__name__}: {exc}"],
                })

        remaining = _managed_open(path)
        if remaining:
            _event("DISARM_REQUESTED_PENDING_EXIT", {
                "managed_open": remaining, "close_results": close_results,
            })
            return {
                "armed": True,
                "allow_new_entries": False,
                "pending_exit": True,
                "managed_open": remaining,
                "close_results": close_results,
                "policy": load_policy(),
            }

        p = save_policy({"live_execution": False, "allow_new_entries": False})
        save_futures_policy({"live_execution": False})
        _event("DISARM_AFTER_IMMEDIATE_CLOSE", {"close_results": close_results})
        return {
            "armed": False,
            "allow_new_entries": False,
            "pending_exit": False,
            "close_results": close_results,
            "policy": p,
        }

    p = save_policy({"live_execution": False, "allow_new_entries": False})
    save_futures_policy({"live_execution": False})
    _event("DISARM", {})
    return {
        "armed": False,
        "allow_new_entries": False,
        "pending_exit": False,
        "policy": p,
    }


def _candidate(perp: str, fixed: str) -> dict[str, Any] | None:
    scan = scan_opportunities(persist=False)
    for x in scan.get("eligible", []):
        if str(x.get("perp_symbol")) == perp and str(x.get("fixed_symbol")) == fixed and x.get("eligible"):
            return x
    return None


def _open_paper_candidates(path: Path = RV_DB_PATH) -> list[dict[str, Any]]:
    init_live_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT p.id,p.root,p.perp_symbol,p.fixed_symbol,p.direction
               FROM rv_paper_pairs p
               LEFT JOIN rv_live_pairs l ON l.paper_id=p.id
               WHERE p.status='OPEN' AND l.paper_id IS NULL
               ORDER BY p.opened_ms"""
        ).fetchall()
    return [
        {
            "paper_id": int(r[0]), "root": r[1], "perp_symbol": r[2],
            "fixed_symbol": r[3], "direction": r[4],
        }
        for r in rows
    ]


def _all_managed_targets(path: Path = RV_DB_PATH) -> list[dict[str, Any]]:
    init_live_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT paper_id,root,perp_symbol,fixed_symbol,direction,size_base
               FROM rv_live_pairs
               WHERE status IN ('OPEN','CLOSE_ERROR','OPENING','OPEN_ERROR')
               ORDER BY opened_ms"""
        ).fetchall()
    return [
        {
            "paper_id": int(r[0]), "root": r[1], "perp_symbol": r[2],
            "fixed_symbol": r[3], "direction": r[4], "size_base": float(r[5]),
        }
        for r in rows
    ]


def _closed_live_targets(path: Path = RV_DB_PATH) -> list[dict[str, Any]]:
    init_live_db(path)
    with sqlite3.connect(path) as con:
        rows = con.execute(
            """SELECT l.paper_id,l.root,l.perp_symbol,l.fixed_symbol,l.direction,l.size_base
               FROM rv_live_pairs l
               JOIN rv_paper_pairs p ON p.id=l.paper_id
               WHERE l.status IN ('OPEN','CLOSE_ERROR') AND p.status='CLOSED'
               ORDER BY p.closed_ms"""
        ).fetchall()
    return [
        {
            "paper_id": int(r[0]), "root": r[1], "perp_symbol": r[2],
            "fixed_symbol": r[3], "direction": r[4], "size_base": float(r[5]),
        }
        for r in rows
    ]


def _delta(before: dict[str, float], after: dict[str, float], symbol: str) -> float:
    s = symbol.upper()
    return float(after.get(s, 0.0)) - float(before.get(s, 0.0))


def _flatten_delta(symbol: str, signed_delta: float) -> dict[str, Any] | None:
    if abs(signed_delta) <= 0:
        return None
    side = "sell" if signed_delta > 0 else "buy"
    size = round_size_down(symbol, abs(signed_delta))
    if size <= 0:
        return None
    return place_order(symbol, side, size, reduce_only=True)


def _target_size(candidate: dict[str, Any]) -> float:
    p = load_policy()
    pxs = [
        float(candidate["perp_bid"]), float(candidate["perp_ask"]),
        float(candidate["fixed_bid"]), float(candidate["fixed_ask"]),
    ]
    ref = max(pxs)
    raw = float(p["target_notional_usd_per_leg"]) / ref
    size = round_size_down(str(candidate["fixed_symbol"]), raw)
    if size <= 0:
        raise RuntimeError("Live target notional is below contract minimum")
    # Both legs share the same base-unit minimum for the supported roots.
    if round_size_down(str(candidate["perp_symbol"]), size) != size:
        raise RuntimeError("Perp/fixed base size steps do not align")
    return size


def open_live_pair(row: dict[str, Any], path: Path = RV_DB_PATH) -> dict[str, Any]:
    rdy = readiness(path)
    if not rdy.get("safe_to_arm") or not rdy.get("live_execution"):
        raise RuntimeError("RV live is not armed/readiness-passed")
    if _managed_open(path):
        raise RuntimeError("Only one live pair is allowed")

    perp = str(row["perp_symbol"])
    fixed = str(row["fixed_symbol"])
    cand = _candidate(perp, fixed)
    if not cand:
        raise RuntimeError("Paper pair is no longer currently eligible on live quotes")

    size = _target_size(cand)
    direction = str(row["direction"])
    if direction == "LONG_PERP_SHORT_FIXED":
        fixed_side, perp_side = "sell", "buy"
        fixed_sign, perp_sign = -1, 1
    elif direction == "SHORT_PERP_LONG_FIXED":
        fixed_side, perp_side = "buy", "sell"
        fixed_sign, perp_sign = 1, -1
    else:
        raise RuntimeError("Unknown RV direction")

    client = client_from_env()
    before = position_map(client.open_positions())

    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT INTO rv_live_pairs(
                paper_id,opened_ms,root,perp_symbol,fixed_symbol,direction,size_base,
                target_notional_usd,status
            ) VALUES(?,?,?,?,?,?,?,?, 'OPENING')""",
            (
                int(row["paper_id"]), _now_ms(), row["root"], perp, fixed, direction,
                size, float(load_policy()["target_notional_usd_per_leg"]),
            ),
        )

    fixed_result: dict[str, Any] | None = None
    perp_result: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None

    try:
        # Fixed first; the perpetual leg is normally the more liquid emergency hedge.
        fixed_result = place_order(fixed, fixed_side, size, reduce_only=False)
        if not fixed_result.get("submitted_live"):
            raise RuntimeError("Fixed leg was not submitted live")
        time.sleep(0.8)
        after_fixed = position_map(client.open_positions())
        fixed_delta = _delta(before, after_fixed, fixed)
        if fixed_delta * fixed_sign <= 0:
            raise RuntimeError("Fixed leg position delta not confirmed")
        actual_size = round_size_down(fixed, abs(fixed_delta))
        if actual_size <= 0:
            raise RuntimeError("Fixed leg confirmed size is below minimum")

        try:
            perp_result = place_order(perp, perp_side, actual_size, reduce_only=False)
            if not perp_result.get("submitted_live"):
                raise RuntimeError("Perpetual hedge was not submitted live")
        except Exception:
            compensation = _flatten_delta(fixed, fixed_delta)
            raise

        time.sleep(0.8)
        after_both = position_map(client.open_positions())
        perp_delta = _delta(before, after_both, perp)
        if perp_delta * perp_sign <= 0:
            compensation = _flatten_delta(fixed, fixed_delta)
            raise RuntimeError("Perpetual hedge position delta not confirmed")

        perp_size = round_size_down(perp, abs(perp_delta))
        tolerance = max(round_size_down(perp, actual_size), actual_size) * 1e-6
        if abs(perp_size - actual_size) > tolerance:
            c1 = _flatten_delta(perp, perp_delta)
            c2 = _flatten_delta(fixed, fixed_delta)
            compensation = {"perp": c1, "fixed": c2}
            raise RuntimeError("Two live legs are not equal-base sized")

        with sqlite3.connect(path) as con:
            con.execute(
                """UPDATE rv_live_pairs
                   SET size_base=?,open_fixed_result=?,open_perp_result=?,
                       compensation_result=?,status='OPEN',error=NULL
                   WHERE paper_id=?""",
                (
                    actual_size,
                    json.dumps(fixed_result, default=str),
                    json.dumps(perp_result, default=str),
                    json.dumps(compensation, default=str) if compensation else None,
                    int(row["paper_id"]),
                ),
            )
        out = {"paper_id": row["paper_id"], "status": "OPEN", "size_base": actual_size}
        _event("OPEN", out)
        return out
    except Exception as exc:
        # Last-resort cleanup: compare with the pre-trade snapshot and flatten
        # every delta attributable to this attempted pair. This is intentionally
        # limited to the two symbols selected by the bridge.
        cleanup = None
        try:
            after_error = position_map(client.open_positions())
            perp_delta_err = _delta(before, after_error, perp)
            fixed_delta_err = _delta(before, after_error, fixed)
            cleanup = {
                "perp": _flatten_delta(perp, perp_delta_err) if abs(perp_delta_err) > 0 else None,
                "fixed": _flatten_delta(fixed, fixed_delta_err) if abs(fixed_delta_err) > 0 else None,
            }
        except Exception as cleanup_exc:
            cleanup = {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"}
        residual = {}
        try:
            after_cleanup = position_map(client.open_positions())
            residual = {
                "perp": _delta(before, after_cleanup, perp),
                "fixed": _delta(before, after_cleanup, fixed),
            }
        except Exception as residual_exc:
            residual = {"error": f"{type(residual_exc).__name__}: {residual_exc}"}

        residual_open = any(
            abs(float(residual.get(k, 0.0))) > 0
            for k in ("perp", "fixed")
            if isinstance(residual.get(k, 0.0), (int, float))
        )
        status = "OPEN_ERROR" if residual_open else "FAILED_FLAT"
        with sqlite3.connect(path) as con:
            con.execute(
                """UPDATE rv_live_pairs
                   SET open_fixed_result=?,open_perp_result=?,compensation_result=?,
                       status=?,error=?
                   WHERE paper_id=?""",
                (
                    json.dumps(fixed_result, default=str),
                    json.dumps(perp_result, default=str),
                    json.dumps({"first": compensation, "cleanup": cleanup, "residual": residual}, default=str),
                    status,
                    f"{type(exc).__name__}: {exc}",
                    int(row["paper_id"]),
                ),
            )
        _event("OPEN_ERROR", {
            "paper_id": row["paper_id"], "error": f"{type(exc).__name__}: {exc}",
            "compensation": compensation, "cleanup": cleanup, "residual": residual,
            "status": status,
        })
        raise


def close_live_pair(row: dict[str, Any], path: Path = RV_DB_PATH) -> dict[str, Any]:
    client = client_from_env()
    positions = position_map(client.open_positions())
    fixed = str(row["fixed_symbol"])
    perp = str(row["perp_symbol"])
    size = float(row["size_base"])

    fixed_pos = float(positions.get(fixed.upper(), 0.0))
    perp_pos = float(positions.get(perp.upper(), 0.0))
    fixed_result = None
    perp_result = None
    errors: list[str] = []

    # Close fixed first, then the liquid perpetual hedge.
    if abs(fixed_pos) > 0:
        try:
            fixed_result = place_order(
                fixed, "sell" if fixed_pos > 0 else "buy",
                round_size_down(fixed, min(abs(fixed_pos), size)), reduce_only=True,
            )
        except Exception as exc:
            errors.append(f"fixed:{type(exc).__name__}:{exc}")
    if abs(perp_pos) > 0:
        try:
            perp_result = place_order(
                perp, "sell" if perp_pos > 0 else "buy",
                round_size_down(perp, min(abs(perp_pos), size)), reduce_only=True,
            )
        except Exception as exc:
            errors.append(f"perp:{type(exc).__name__}:{exc}")

    status = "CLOSED" if not errors else "CLOSE_ERROR"
    with sqlite3.connect(path) as con:
        con.execute(
            """UPDATE rv_live_pairs
               SET closed_ms=?,close_fixed_result=?,close_perp_result=?,status=?,error=?
               WHERE paper_id=?""",
            (
                _now_ms(), json.dumps(fixed_result, default=str), json.dumps(perp_result, default=str),
                status, ";".join(errors) if errors else None, int(row["paper_id"]),
            ),
        )
    out = {"paper_id": row["paper_id"], "status": status, "errors": errors}
    _event("CLOSE", out)
    return out


def run_once(path: Path = RV_DB_PATH) -> dict[str, Any]:
    rdy = readiness(path)
    policy = load_policy()
    if not rdy.get("live_execution"):
        return {"active": False, "readiness": rdy, "opened": [], "closed": []}

    # Existing live exposure is always managed before evaluating any new-entry
    # evidence gate. A deteriorating paper metric must never prevent an exit.
    closed: list[dict[str, Any]] = []
    for row in _closed_live_targets(path):
        closed.append(close_live_pair(row, path))

    managed_after = _managed_open(path)
    if managed_after and not policy.get("allow_new_entries"):
        for row in _all_managed_targets(path):
            try:
                closed.append(close_live_pair(row, path))
            except Exception as exc:
                closed.append({
                    "paper_id": row["paper_id"],
                    "status": "CLOSE_ERROR",
                    "errors": [f"{type(exc).__name__}: {exc}"],
                })
        managed_after = _managed_open(path)

    if managed_after:
        return {
            "active": True,
            "management_only": True,
            "allow_new_entries": bool(policy.get("allow_new_entries")),
            "readiness": readiness(path),
            "opened": [],
            "closed": closed,
        }

    if not policy.get("allow_new_entries"):
        p = save_policy({"live_execution": False, "allow_new_entries": False})
        save_futures_policy({"live_execution": False})
        _event("DISARM_COMPLETED_AFTER_EXIT", {})
        return {
            "active": False,
            "auto_disarmed": True,
            "allow_new_entries": False,
            "policy": p,
            "readiness": readiness(path),
            "opened": [],
            "closed": closed,
        }

    rdy_after_close = readiness(path)
    if not rdy_after_close.get("safe_to_arm"):
        set_live_execution(False, path)
        return {
            "active": False,
            "auto_disarmed": True,
            "readiness": rdy_after_close,
            "opened": [],
            "closed": closed,
        }

    opened: list[dict[str, Any]] = []
    for row in _open_paper_candidates(path)[:1]:
        opened.append(open_live_pair(row, path))

    return {
        "active": True,
        "allow_new_entries": True,
        "readiness": readiness(path),
        "opened": opened,
        "closed": closed,
    }


def daemon(path: Path = RV_DB_PATH) -> None:
    init_live_db(path)
    while True:
        try:
            result = run_once(path)
            _event("HEARTBEAT", {
                "active": result.get("active"),
                "blockers": (result.get("readiness") or {}).get("blockers", []),
            })
        except KeyboardInterrupt:
            break
        except Exception as exc:
            _event("LOOP_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
        time.sleep(float(load_policy()["poll_s"]))


def selftest() -> dict[str, Any]:
    test_db = Path("data/rv_live_selftest.db")
    if test_db.exists():
        try:
            test_db.unlink()
        except Exception:
            pass
    init_live_db(test_db)

    ids: list[int] = []
    with sqlite3.connect(test_db) as con:
        for i in range(20):
            pnl = 12.0 if i % 2 == 0 else -5.0
            net_bps = 16.0 if pnl > 0 else -7.0
            cur = con.execute(
                """INSERT INTO rv_paper_pairs(
                    opened_ms,closed_ms,root,perp_symbol,fixed_symbol,direction,
                    notional_per_leg_czk,entry_perp_px,entry_fixed_px,
                    entry_edge_bps,entry_funding_bps_h,neutral_units,
                    pnl_czk,net_pnl_bps,exit_reason,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    1_000_000 + i * 10_000,
                    1_005_000 + i * 10_000,
                    "XBTUSD",
                    "PF_XBTUSD",
                    "FF_XBTUSD_271231",
                    "LONG_PERP_SHORT_FIXED",
                    100.0,
                    100.0,
                    100.5,
                    10.0,
                    0.0,
                    0.995,
                    pnl,
                    net_bps,
                    "SELFTEST",
                    "CLOSED",
                ),
            )
            ids.append(int(cur.lastrowid))

        con.execute(
            """INSERT INTO rv_live_pairs(
                paper_id,opened_ms,root,perp_symbol,fixed_symbol,direction,
                size_base,target_notional_usd,status,error
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                ids[0], 2_000_000, "XBTUSD", "PF_XBTUSD", "FF_XBTUSD_271231",
                "LONG_PERP_SHORT_FIXED", 0.0001, 10.0, "CLOSE_ERROR", "synthetic",
            ),
        )

    evidence = paper_evidence(test_db)
    retry_targets = _closed_live_targets(test_db)
    lot_checks = {
        "btc": round_size_down("PF_XBTUSD", 0.00019) == 0.0001,
        "eth": round_size_down("PF_ETHUSD", 0.0019) == 0.001,
        "sol": round_size_down("PF_SOLUSD", 0.019) == 0.01,
    }
    checks = {
        "twenty_closed_pairs": evidence["closed_pairs"] == 20,
        "positive_net_pnl": evidence["net_pnl_czk"] > 0,
        "profit_factor_above_floor": evidence["profit_factor"] > 1.2,
        "drawdown_below_five_pct": evidence["max_drawdown_pct"] < 5.0,
        "close_error_is_retried": len(retry_targets) == 1 and retry_targets[0]["paper_id"] == ids[0],
        "lot_rounding": all(lot_checks.values()),
        "live_default_disarmed": not bool(load_policy().get("live_execution", False)),
    }
    try:
        test_db.unlink()
    except Exception:
        pass
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "evidence": evidence,
        "lot_checks": lot_checks,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readiness", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--disarm", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    args = ap.parse_args()
    if args.readiness or args.status:
        r = readiness()
        print(json.dumps(r, indent=2, default=str))
        if args.readiness and not r.get("safe_to_arm"):
            raise SystemExit(2)
    elif args.arm:
        print(json.dumps(set_live_execution(True), indent=2, default=str))
    elif args.disarm:
        print(json.dumps(set_live_execution(False), indent=2, default=str))
    elif args.daemon:
        daemon()
    else:
        print(json.dumps(run_once(), indent=2, default=str))


if __name__ == "__main__":
    main()
