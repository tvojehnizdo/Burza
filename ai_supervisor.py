from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from live_bridge import BRIDGE
from kraken_live_control import (
    account_snapshot,
    cancel_all_orders,
    load_policy,
    save_policy,
)

V4 = os.getenv("V4_BASE_URL", "http://127.0.0.1:8765")
MODEL = os.getenv("OPENAI_SUPERVISOR_MODEL", "gpt-5.6-terra")
AUTO_INTERVAL = int(os.getenv("SUPERVISOR_INTERVAL_S", "300"))
AUTO = os.getenv("SUPERVISOR_AUTO", "0").lower() in {"1", "true", "yes", "on"}
CONTROL = os.getenv("SUPERVISOR_CONTROL", "1").lower() in {"1", "true", "yes", "on"}
LOG_PATH = Path(os.getenv("SUPERVISOR_LOG", "reports/ai-supervisor.jsonl"))
EXPECTED_V4_BUILD = "4.1-cost-aware"

app = FastAPI(title="IMPULSE AI Supervisor", version="1.0")


class AskBody(BaseModel):
    instruction: str


def _get(path: str) -> Any:
    r = requests.get(V4 + path, timeout=8)
    r.raise_for_status()
    return r.json()


def _post(path: str) -> Any:
    r = requests.post(V4 + path, timeout=8)
    r.raise_for_status()
    return r.json()


def system_context() -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    for name, path in [
        ("v4_status", "/api/v4/status"),
        ("models", "/api/v4/models"),
        ("paper", "/api/v4/paper"),
    ]:
        try:
            ctx[name] = _get(path)
        except Exception as exc:
            ctx[name] = {"error": f"{type(exc).__name__}: {exc}"}

    v4_status = ctx.get("v4_status") if isinstance(ctx.get("v4_status"), dict) else {}
    actual_build = v4_status.get("build") or v4_status.get("version")
    ctx["runtime_guard"] = {
        "expected_build": EXPECTED_V4_BUILD,
        "actual_build": actual_build,
        "stale_runtime": actual_build != EXPECTED_V4_BUILD,
        "required_action": (
            "Restart the V4 Python process from the current main branch before judging model quality."
            if actual_build != EXPECTED_V4_BUILD else "none"
        ),
    }

    try:
        ctx["kraken_account"] = account_snapshot()
    except Exception as exc:
        ctx["kraken_account"] = {"error": f"{type(exc).__name__}: {exc}"}

    readiness = Path("reports/kraken-readiness-latest.json")
    if readiness.exists():
        try:
            ctx["readiness"] = json.loads(readiness.read_text(encoding="utf-8-sig"))
        except Exception:
            pass

    try:
        if os.getenv("KRAKEN_FUTURES_API_KEY") and os.getenv("KRAKEN_FUTURES_API_SECRET"):
            import futures_private
            ctx["kraken_futures"] = futures_private.readiness()
        else:
            ctx["kraken_futures"] = {"configured": False}
    except Exception as exc:
        ctx["kraken_futures"] = {"error": f"{type(exc).__name__}: {exc}"}

    ctx["policy"] = load_policy()
    return ctx


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Model did not return JSON")
    return json.loads(m.group(0))


def ask_model(instruction: str, context: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    prompt = f"""
You are the local supervisory layer for IMPULSE MAX 5K.
Goal: improve robustness and net profitability without fabricating edge.

Hard rules:
- NEVER request, enable, or use withdrawals.
- NEVER request wallet-transfer permission.
- NEVER enable live_execution yourself.
- You may turn live_execution OFF, reduce leverage/risk, cancel orders,
  pause/resume V4, or change safe supervisor policy values.
- Trading entries/exits are owned by the deterministic Pulse Engine, not by you.
- If evidence is weak, prefer no change.
- Do not infer profitability from tiny samples.
- Check runtime_guard first. If stale_runtime=true, explicitly flag the stale
  V4 process and do not treat old horizons/cost settings as the current model.
- Respond with JSON only.

Allowed actions:
{{"type":"v4_start"}}
{{"type":"v4_stop"}}
{{"type":"cancel_all"}}
{{"type":"set_policy","patch":{{...}}}}
{{"type":"none"}}

For set_policy you may change only:
allow_margin, allow_cancel_all, max_leverage,
max_order_notional_pct_equity, max_total_open_orders,
require_positive_consensus, live_execution.
If using live_execution it may only be false.

Return:
{{
  "summary":"short Czech explanation",
  "risk":"low|medium|high",
  "actions":[...],
  "observations":[...]
}}

User instruction:
{instruction}

Current system context:
{json.dumps(context, ensure_ascii=False, default=str)[:60000]}
"""
    response = client.responses.create(
        model=MODEL,
        input=prompt,
    )
    return _extract_json(response.output_text)


def apply_actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for action in plan.get("actions") or []:
        typ = action.get("type")
        try:
            if typ in (None, "none"):
                results.append({"type": "none", "ok": True})
            elif not CONTROL:
                results.append({"type": typ, "ok": False, "reason": "SUPERVISOR_CONTROL=0"})
            elif typ == "v4_start":
                results.append({"type": typ, "ok": True, "result": _post("/api/v4/start")})
            elif typ == "v4_stop":
                results.append({"type": typ, "ok": True, "result": _post("/api/v4/stop")})
            elif typ == "cancel_all":
                results.append({"type": typ, "ok": True, "result": cancel_all_orders()})
            elif typ == "futures_deadman":
                import futures_private
                ready = futures_private.readiness()
                if not ready.get("safe_to_arm"):
                    results.append({
                        "type": typ,
                        "ok": False,
                        "reason": "Futures API key permissions are unsafe: transfer/withdrawal access must be NO_ACCESS",
                    })
                    continue
                timeout_s = int(action.get("timeout_s", 60))
                timeout_s = max(10, min(timeout_s, 300))
                results.append({
                    "type": typ,
                    "ok": True,
                    "result": futures_private.client_from_env().deadman(timeout_s),
                })
            elif typ == "set_policy":
                patch = dict(action.get("patch") or {})
                if patch.get("live_execution") is True:
                    patch["live_execution"] = False
                # AI may reduce, never increase, leverage/risk.
                current = load_policy()
                if "max_leverage" in patch:
                    patch["max_leverage"] = min(int(patch["max_leverage"]), int(current["max_leverage"]))
                if "max_order_notional_pct_equity" in patch:
                    patch["max_order_notional_pct_equity"] = min(
                        float(patch["max_order_notional_pct_equity"]),
                        float(current["max_order_notional_pct_equity"]),
                    )
                results.append({"type": typ, "ok": True, "result": save_policy(patch)})
            else:
                results.append({"type": typ, "ok": False, "reason": "not allowed"})
        except Exception as exc:
            results.append({"type": typ, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return results


def run_supervision(instruction: str) -> dict[str, Any]:
    context = system_context()
    plan = ask_model(instruction, context)
    applied = apply_actions(plan)
    result = {
        "time": int(time.time()),
        "model": MODEL,
        "instruction": instruction,
        "plan": plan,
        "applied": applied,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    return result


class SupervisorLoop:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last: dict[str, Any] | None = None
        self.error: str | None = None

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="ai-supervisor")
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.last = run_supervision(
                    "Proveď periodickou kontrolu systému. Zasahuj jen pokud je problém, "
                    "riziko je zbytečně vysoké nebo je nutná bezpečnostní změna."
                )
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(AUTO_INTERVAL)


LOOP = SupervisorLoop()


@app.on_event("startup")
def startup():
    BRIDGE.start()
    if AUTO:
        LOOP.start()


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset='utf-8'><title>IMPULSE AI Supervisor</title>
<style>body{font-family:system-ui;max-width:1000px;margin:30px auto;background:#0b1020;color:#e8eefc;padding:0 16px}
textarea,pre{width:100%;box-sizing:border-box;background:#141b31;color:#e8eefc;border:0;border-radius:10px;padding:12px}
button{padding:10px 14px;margin:6px 3px}</style></head><body>
<h1>IMPULSE AI Supervisor</h1>
<p>AI může kontrolovat systém, měnit jen povolené bezpečné parametry, pause/resume a cancel-all.
Nemůže zapnout LIVE, převody ani výběry.</p>
<textarea id='q' rows='4'>Zkontroluj celý systém a navrhni nebo proveď jen smysluplné změny.</textarea>
<button onclick='ask()'>AI kontrola</button><button onclick='status()'>Status</button>
<pre id='o'>Ready.</pre>
<script>
async function ask(){o.textContent='Running...';let r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruction:q.value})});o.textContent=JSON.stringify(await r.json(),null,2)}
async function status(){let r=await fetch('/api/status');o.textContent=JSON.stringify(await r.json(),null,2)}
</script></body></html>"""


@app.get("/api/status")
def status():
    return {
        "ok": True,
        "model": MODEL,
        "auto": AUTO,
        "control": CONTROL,
        "loop_running": bool(LOOP.thread and LOOP.thread.is_alive()),
        "loop_error": LOOP.error,
        "policy": load_policy(),
        "live_bridge": BRIDGE.status(),
        "context": system_context(),
    }


@app.post("/api/ask")
def ask(body: AskBody):
    return run_supervision(body.instruction)


@app.post("/api/auto/start")
def auto_start():
    return {"started": LOOP.start()}


@app.post("/api/auto/stop")
def auto_stop():
    LOOP.stop()
    return {"stopping": True}
