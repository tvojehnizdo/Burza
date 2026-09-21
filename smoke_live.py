import json
import engine

PARAMS = {
    "fast": 8, "slow": 24, "zwin": 30, "threshold": 0.64,
    "stop_atr": 1.2, "take_atr": 2.4, "max_hold": 25,
    "max_alloc": 0.55, "cost_multiple": 1.5,
}

report = {"selftest": engine.selftest_report(), "spot": {}, "futures": {}}
universe = engine.kraken_universe(12)
report["universe"] = universe

for symbol in ["XBTUSD", "ETHUSD", "SOLUSD"]:
    df = engine.klines(symbol, limit=720)
    report["spot"][symbol] = {
        "bars": len(df),
        "latest_pulse": engine.live_pulse(symbol),
        "recent_replay_spot_costs": {k:v for k,v in engine.run_bt(df, symbol, PARAMS).items() if k != "ledger"},
    }

print(json.dumps(report, indent=2, default=str))
if not report["selftest"]["ok"]:
    raise SystemExit(2)

for symbol in engine.FUTURES_SYMBOLS:
    try:
        df = engine.futures_klines(symbol, 720)
        report["futures"][symbol] = {
            "bars": len(df),
            "latest_pulse": engine.live_futures_pulse(symbol),
            "recent_replay_futures_costs": {k:v for k,v in engine.run_bt(df, symbol, PARAMS, "futures").items() if k != "ledger"},
        }
    except Exception as exc:
        report["futures"][symbol] = {"error": str(exc)}
