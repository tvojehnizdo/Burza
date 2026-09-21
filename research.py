import itertools
import json
import engine

SYMBOLS = engine.FUTURES_SYMBOLS
COUNT = 5000

grid = []
for threshold, stop_atr, take_atr, hold, edge_multiple in itertools.product(
    [0.64, 0.72, 0.80],
    [1.0, 1.5],
    [2.0, 3.0],
    [15, 40],
    [1.2, 1.5],
):
    grid.append({
        "fast": 8,
        "slow": 24,
        "zwin": 30,
        "threshold": threshold,
        "stop_atr": stop_atr,
        "take_atr": take_atr,
        "max_hold": hold,
        "max_alloc": 0.55,
        "cost_multiple": 1.5,
        "edge_multiple": edge_multiple,
    })

report = {"count": COUNT, "candidates_per_symbol": len(grid), "symbols": {}, "robust": []}

for symbol in SYMBOLS:
    try:
        df = engine.futures_klines(symbol, COUNT)
        n = len(df)
        a, b = int(n * 0.60), int(n * 0.80)
        train = df.iloc[:a].reset_index(drop=True)
        valid = df.iloc[a:b].reset_index(drop=True)
        holdout = df.iloc[b:].reset_index(drop=True)

        trained = []
        for p in grid:
            tr = engine.run_bt(train, symbol, p, "futures")
            if tr["trades"] >= 3:
                trained.append((engine.objective(tr), p, tr))
        trained.sort(key=lambda x: x[0], reverse=True)

        selected = []
        for _, p, tr in trained[:10]:
            va = engine.run_bt(valid, symbol, p, "futures")
            # Selection can use train+validation only. Holdout stays untouched.
            if va["trades"] >= 2 and tr["return_pct"] > 0 and va["return_pct"] > 0 and tr["profit_factor"] > 1.05 and va["profit_factor"] > 1.05:
                selected.append((engine.objective(va), p, tr, va))
        selected.sort(key=lambda x: x[0], reverse=True)

        if not selected:
            report["symbols"][symbol] = {"bars": n, "status": "NO_VALIDATED_EDGE"}
            continue

        _, p, tr, va = selected[0]
        ho = engine.run_bt(holdout, symbol, p, "futures")
        row = {
            "bars": n,
            "status": "VALIDATED_CANDIDATE",
            "params": p,
            "train": {k:v for k,v in tr.items() if k != "ledger"},
            "validation": {k:v for k,v in va.items() if k != "ledger"},
            "holdout": {k:v for k,v in ho.items() if k != "ledger"},
        }
        row["holdout_pass"] = bool(
            ho["trades"] >= 2
            and ho["return_pct"] > 0
            and ho["profit_factor"] > 1.0
            and ho["expectancy_czk"] > 0
        )
        report["symbols"][symbol] = row
        if row["holdout_pass"]:
            report["robust"].append(symbol)
    except Exception as exc:
        report["symbols"][symbol] = {"status": "ERROR", "error": str(exc)}

print(json.dumps(report, indent=2, default=str))
