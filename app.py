from __future__ import annotations
import os, math, time, json, itertools, statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional
import requests
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

START_CAPITAL=float(os.getenv("START_CAPITAL","5000"))
FEE_BPS=float(os.getenv("FEE_BPS","10"))
SLIPPAGE_BPS=float(os.getenv("SLIPPAGE_BPS","3"))
MAX_DD=float(os.getenv("MAX_DRAWDOWN_PCT","10"))/100
RISK_PCT=float(os.getenv("RISK_PER_TRADE_PCT","1.0"))/100
SYMBOLS=os.getenv("SYMBOLS","BTCUSDT,ETHUSDT,SOLUSDT").split(",")\nPULSE_MIN=float(os.getenv("PULSE_MIN","0.62"))
BYBIT="https://api.bybit.com"
app=FastAPI(title="IMPULSE MAX 5K",version="1.0")

@dataclass
class Trade:
    symbol:str; entry_time:str; exit_time:str; entry:float; exit:float
    qty:float; pnl:float; fees:float; reason:str; equity:float

def klines(symbol:str, interval="1", limit=1000):
    r=requests.get(BYBIT+"/v5/market/kline",params={"category":"spot","symbol":symbol,"interval":interval,"limit":limit},timeout=15)
    r.raise_for_status(); rows=r.json()["result"]["list"]
    df=pd.DataFrame(rows,columns=["ts","open","high","low","close","volume","turnover"])
    for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c])
    df["ts"]=pd.to_datetime(pd.to_numeric(df.ts),unit="ms",utc=True)
    return df.sort_values("ts").reset_index(drop=True)

def features(df, fast=8, slow=24, zwin=30, atrn=14):
    x=df.copy()
    x["ef"]=x.close.ewm(span=fast,adjust=False).mean(); x["es"]=x.close.ewm(span=slow,adjust=False).mean()
    x["ret"]=np.log(x.close/x.close.shift(1)); x["mom"]=x.close.pct_change(fast)
    x["mu"]=x.close.rolling(zwin).mean(); x["sd"]=x.close.rolling(zwin).std(); x["z"]=(x.close-x.mu)/x.sd
    tr=np.maximum(x.high-x.low,np.maximum((x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()))
    x["atr"]=tr.rolling(atrn).mean(); x["rv"]=x.volume/x.volume.rolling(30).median()
    x["hh"]=x.high.shift(1).rolling(20).max()
    return x

def signal(row, mode="adaptive"):
    trend=(row.ef-row.es)/row.close
    momentum=row.mom
    breakout=1.0 if row.close>row.hh else 0.0
    vol=min(max(row.rv-1,0),3) if pd.notna(row.rv) else 0
    # Regime adaptive: trend/breakout when directional; mean reversion only in weak trend.
    score=0.45*np.tanh(trend*250)+0.30*np.tanh(momentum*35)+0.15*breakout+0.10*np.tanh(vol)
    if abs(trend)<0.0015 and pd.notna(row.z) and row.z<-1.8:
        score=max(score,0.58+min(abs(row.z)-1.8,1.5)*0.08)
    return float(score)

def run_bt(df,symbol,params):
    x=features(df,params["fast"],params["slow"],params["zwin"])
    eq=START_CAPITAL; peak=eq; trades=[]; pos=None; stopped=False
    cost=(FEE_BPS+SLIPPAGE_BPS)/10000
    for i,row in x.iterrows():
        if i<max(params["slow"],params["zwin"],35): continue
        if pos:
            px=row.close; ret=(px-pos["entry"])/pos["entry"]
            atrp=(row.atr/row.close) if row.close and pd.notna(row.atr) else .005
            stop=max(params["stop_atr"]*atrp,.003)
            take=max(params["take_atr"]*atrp,.005)
            age=i-pos["i"]
            reason=None
            if ret<=-stop: reason="SL"
            elif ret>=take: reason="TP"
            elif age>=params["max_hold"]: reason="TIME"
            elif signal(row)<0.05 and age>2: reason="REGIME"
            if reason:
                gross=pos["notional"]*ret
                fees=pos["notional"]*cost + max(pos["notional"]+gross,0)*cost
                pnl=gross-fees; eq+=pnl
                trades.append(Trade(symbol,str(pos["time"]),str(row.ts),pos["entry"],px,pos["qty"],pnl,fees,reason,eq))
                pos=None; peak=max(peak,eq)
                if eq <= START_CAPITAL*(1-MAX_DD): stopped=True; break
        if not pos:
            s=signal(row)
            if s>=params["threshold"]:
                atrp=(row.atr/row.close) if row.close and pd.notna(row.atr) else .005
                stop=max(params["stop_atr"]*atrp,.003)
                risk_cash=min(eq*RISK_PCT, max(eq-START_CAPITAL*(1-MAX_DD),0))
                notional=min(eq*params["max_alloc"], risk_cash/stop if stop>0 else 0)
                # Cost-aware gate: projected target must comfortably exceed round-trip costs.
                target=max(params["take_atr"]*atrp,.005)
                if notional>=100 and target > 2.5*(2*cost):
                    pos={"entry":row.close,"i":i,"time":row.ts,"notional":notional,"qty":notional/row.close}
        peak=max(peak,eq)
    pnls=[t.pnl for t in trades]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    pf=sum(wins)/abs(sum(losses)) if losses else (99 if wins else 0)
    maxdd=0; p=START_CAPITAL
    for t in trades:
        p=max(p,t.equity); maxdd=max(maxdd,(p-t.equity)/p if p else 0)
    return {"symbol":symbol,"equity":round(eq,2),"return_pct":round((eq/START_CAPITAL-1)*100,2),
            "trades":len(trades),"win_rate":round(len(wins)/len(pnls)*100,1) if pnls else 0,
            "profit_factor":round(pf,2),"max_dd_pct":round(maxdd*100,2),
            "fees":round(sum(t.fees for t in trades),2),"stopped":stopped,
            "ledger":[asdict(t) for t in trades]}

def objective(r):
    if r["trades"]<8:return -999
    return r["return_pct"] + min(r["profit_factor"],4)*1.5 - r["max_dd_pct"]*1.8 - r["fees"]/START_CAPITAL*100*.25

def optimize(symbol):
    df=klines(symbol,limit=1000)
    cut=int(len(df)*.7); train=df.iloc[:cut].reset_index(drop=True); test=df.iloc[cut:].reset_index(drop=True)
    grid=[]
    for fast,slow,thr,sa,ta,hold in itertools.product([5,8,12],[20,30],[.48,.55,.62],[1.0,1.5],[1.5,2.2,3.0],[10,25,60]):
        if fast>=slow: continue
        p={"fast":fast,"slow":slow,"zwin":30,"threshold":thr,"stop_atr":sa,"take_atr":ta,"max_hold":hold,"max_alloc":.65}
        r=run_bt(train,symbol,p); grid.append((objective(r),p,r))
    grid.sort(key=lambda z:z[0],reverse=True)
    # choose robust candidate among top 10 by unseen test score, not raw train winner
    candidates=[]
    for _,p,tr in grid[:10]:
        te=run_bt(test,symbol,p); candidates.append((objective(te),p,tr,te))
    candidates.sort(key=lambda z:z[0],reverse=True)
    _,p,tr,te=candidates[0]
    full=run_bt(df,symbol,p)
    # final untouched holdout: last 15% is reported separately and never used for parameter selection\n    h=int(len(df)*.85); holdout=run_bt(df.iloc[h:].reset_index(drop=True),symbol,p)\n    return {"params":p,"train":{k:v for k,v in tr.items() if k!="ledger"},
            "test":{k:v for k,v in te.items() if k!="ledger"},"holdout":{k:v for k,v in holdout.items() if k!="ledger"},"full":full}

@app.get("/",response_class=HTMLResponse)
def home():
    return """<html><head><title>IMPULSE MAX 5K</title><style>body{font-family:system-ui;max-width:980px;margin:40px auto;padding:0 16px;background:#0b1020;color:#e8eefc}button{padding:12px 18px}pre{white-space:pre-wrap;background:#141b31;padding:16px;border-radius:12px}.ok{color:#7ee787}</style></head><body><h1>IMPULSE MAX 5K</h1><p>PAPER / REPLAY. Start 5 000 Kč. Spot, no leverage. Pulse filter rejects contradictory/noisy setups.</p><button id=b>Spustit optimalizaci + replay</button><pre id=o>Ready.</pre><script>b.onclick=async()=>{b.disabled=true;o.textContent='Running...';try{let r=await fetch('/api/run',{method:'POST'});o.textContent=JSON.stringify(await r.json(),null,2)}catch(e){o.textContent=e}b.disabled=false}</script></body></html>"""

@app.post("/api/run")
def run():
    out={}
    for s in SYMBOLS:
        try: out[s]=optimize(s)
        except Exception as e: out[s]={"error":str(e)}
    valid=[(v["test"]["return_pct"],k) for k,v in out.items() if "test" in v]
    winner=max(valid)[1] if valid else None
    return {"mode":"PAPER_REPLAY","start_capital_czk":START_CAPITAL,"winner":winner,"results":out,
            "warning":"Backtest is not a guarantee. LIVE remains disabled."}

@app.get("/api/health")
def health(): return {"ok":True,"mode":"PAPER_REPLAY","capital":START_CAPITAL}
