import asyncio
import json

import websockets

from alpha_discovery import alpha_selftest
from microstructure import recorder_selftest

WS = "wss://ws.kraken.com/v2"


async def live_ws_smoke():
    async with websockets.connect(WS, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {
                "channel": "book",
                "symbol": ["BTC/USD"],
                "depth": 10,
                "snapshot": True,
            },
            "req_id": 99,
        }))
        deadline = asyncio.get_running_loop().time() + 12
        while asyncio.get_running_loop().time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
            if msg.get("channel") == "book" and msg.get("type") == "snapshot":
                data = msg.get("data") or []
                if data and data[0].get("bids") and data[0].get("asks"):
                    return {
                        "ok": True,
                        "symbol": data[0].get("symbol"),
                        "bids": len(data[0].get("bids") or []),
                        "asks": len(data[0].get("asks") or []),
                    }
        raise RuntimeError("No Kraken book snapshot received")


def main():
    recorder = recorder_selftest()
    alpha = alpha_selftest()
    live = asyncio.run(live_ws_smoke())
    report = {"recorder": recorder, "alpha": alpha, "live_ws": live}
    print(json.dumps(report, indent=2, default=str))
    assert recorder["ok"]
    assert alpha["ok"]
    assert live["ok"]


if __name__ == "__main__":
    main()
