"""
logger.py - Trade logging and performance metrics.
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone

import config

# ---------------------------------------------------------------------------
# Standard Python logging setup
# ---------------------------------------------------------------------------

def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger to write to console and a rotating log file."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = os.path.join(config.LOG_DIR, "bot.log")

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Trade CSV logger
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "timestamp",
    "type",
    "symbol",
    "direction",
    "buy_exchange",
    "sell_exchange",
    "amount",
    "buy_price",
    "sell_price",
    "trade_size_usd",
    "net_profit_pct",
    "net_profit_usd",
    "buy_order_id",
    "sell_order_id",
]

_logger = logging.getLogger(__name__)


def log_trade(trade: dict) -> None:
    """Append a completed trade record to the CSV trade log."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    file_exists = os.path.exists(config.TRADES_CSV)

    row = {field: trade.get(field, "") for field in _CSV_FIELDS}
    # Human-readable timestamp
    if row["timestamp"]:
        row["timestamp"] = datetime.fromtimestamp(
            float(row["timestamp"]), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        with open(config.TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        _logger.debug("Trade logged to %s", config.TRADES_CSV)
    except OSError as exc:
        _logger.error("Could not write trade log: %s", exc)


# ---------------------------------------------------------------------------
# Metrics tracker
# ---------------------------------------------------------------------------

class MetricsTracker:
    """Accumulates performance statistics and writes them to a JSON file."""

    def __init__(self) -> None:
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(config.METRICS_JSON):
            try:
                with open(config.METRICS_JSON, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_profit_usd": 0.0,
            "total_loss_usd": 0.0,
            "net_pnl_usd": 0.0,
            "start_time": time.time(),
            "last_updated": time.time(),
        }

    def _save(self) -> None:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        self._data["last_updated"] = time.time()
        try:
            with open(config.METRICS_JSON, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError as exc:
            _logger.error("Could not save metrics: %s", exc)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def record(self, trade: dict) -> None:
        """Update metrics with a completed trade."""
        pnl = trade.get("net_profit_usd", 0.0)
        self._data["total_trades"] += 1
        if pnl >= 0:
            self._data["winning_trades"] += 1
            self._data["total_profit_usd"] += pnl
        else:
            self._data["losing_trades"] += 1
            self._data["total_loss_usd"] += abs(pnl)
        self._data["net_pnl_usd"] += pnl
        self._save()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        d = self._data
        total = d["total_trades"]
        win_rate = (d["winning_trades"] / total * 100) if total else 0.0
        uptime_sec = time.time() - d.get("start_time", time.time())
        uptime_h = uptime_sec / 3600

        return {
            "total_trades": total,
            "win_rate_pct": round(win_rate, 2),
            "net_pnl_usd": round(d["net_pnl_usd"], 4),
            "total_profit_usd": round(d["total_profit_usd"], 4),
            "total_loss_usd": round(d["total_loss_usd"], 4),
            "uptime_hours": round(uptime_h, 2),
        }

    def print_dashboard(self, risk_status: dict) -> None:
        """Print a concise metrics dashboard to the console."""
        s = self.summary()
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(
            f"\n{'=' * 60}\n"
            f"  TRADING BOT DASHBOARD  –  {now}\n"
            f"{'=' * 60}\n"
            f"  Trades       : {s['total_trades']}\n"
            f"  Win rate     : {s['win_rate_pct']:.1f}%\n"
            f"  Net P&L      : {s['net_pnl_usd']:+.4f} USD\n"
            f"  Profit       : {s['total_profit_usd']:.4f} USD\n"
            f"  Loss         : {s['total_loss_usd']:.4f} USD\n"
            f"  Uptime       : {s['uptime_hours']:.1f} h\n"
            f"{'─' * 60}\n"
            f"  Daily loss   : {risk_status.get('daily_loss_usd', 0):.4f} / "
            f"{risk_status.get('daily_loss_limit_usd', 0):.2f} USD\n"
            f"  Open pos.    : {risk_status.get('open_positions', 0)} / "
            f"{risk_status.get('max_positions', 0)}\n"
            f"  Trading      : {'✅ YES' if risk_status.get('trading_allowed') else '🛑 NO'}\n"
            f"{'=' * 60}\n"
        )
