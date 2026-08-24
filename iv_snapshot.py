"""Daily implied-volatility snapshot → Supabase `iv_history`.

For each ticker the owner holds (+ a small core set), fetch the options chain via the
finhub-ticker-proxy worker, take the expiration nearest 30 DTE, solve the at-the-money
implied vol from the call and put mid/last prices, and upsert one row per ticker/day.
The Options page reads the trailing history to compute IV rank / IV percentile — the
one options metric no feed ships, because it needs an accumulated history.

Runs on GitHub Actions (post US close). Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
HOLDINGS_USER_ID. No IBKR/Yahoo secrets — the chain comes through the public worker.
"""
from __future__ import annotations
import datetime as dt
import math
import os
import sys

import requests
from scipy.optimize import brentq
from scipy.stats import norm

WORKER = "https://finhub-ticker-proxy.edenswack1.workers.dev"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
HOLDINGS_USER_ID = os.environ.get("HOLDINGS_USER_ID", "")
CORE = ["SPY", "QQQ"]          # always track the broad market for context
TARGET_DTE = 30
R = 0.045                       # risk-free, matches the app


def bs_price(kind: str, S: float, K: float, T: float, sig: float) -> float:
    d1 = (math.log(S / K) + (R + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if kind == "call":
        return S * norm.cdf(d1) - K * math.exp(-R * T) * norm.cdf(d2)
    return K * math.exp(-R * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def solve_iv(kind: str, price: float, S: float, K: float, T: float):
    if not (price > 0 and S > 0 and K > 0 and T > 0):
        return None
    try:
        return brentq(lambda s: bs_price(kind, S, K, T, s) - price, 1e-4, 5)
    except Exception:
        return None


def _mid(c: dict) -> float:
    b, a = c.get("bid", 0) or 0, c.get("ask", 0) or 0
    return (b + a) / 2 if b > 0 and a > 0 else (c.get("lastPrice", 0) or 0)


def atm_iv(ticker: str):
    """~30-DTE at-the-money implied vol for `ticker`, or (None, None)."""
    y = ticker.replace(".", "-")
    base = requests.get(f"{WORKER}/?options={y}", timeout=30).json()
    r0 = (base.get("optionChain", {}).get("result") or [None])[0]
    if not r0:
        return None, None
    spot = r0.get("quote", {}).get("regularMarketPrice")
    now = dt.datetime.now().timestamp()
    exps = [e for e in r0.get("expirationDates", []) if e >= now]
    if not spot or not exps:
        return None, None
    pick = min(exps, key=lambda e: abs((e - now) / 86400 - TARGET_DTE))
    T = max(1, round((pick - now) / 86400)) / 365
    node = r0.get("options", [{}])[0]
    if node.get("expirationDate") != pick:
        d = requests.get(f"{WORKER}/?options={y}&date={pick}", timeout=30).json()
        node = (d.get("optionChain", {}).get("result") or [{}])[0].get("options", [{}])[0]

    ivs = []
    for kind, arr in (("call", node.get("calls", [])), ("put", node.get("puts", []))):
        if not arr:
            continue
        atm = min(arr, key=lambda c: abs(c.get("strike", 1e9) - spot))
        v = solve_iv(kind, _mid(atm), spot, atm["strike"], T)
        if v and 0.01 < v < 5:
            ivs.append(v)
    if not ivs:
        return None, spot
    return sum(ivs) / len(ivs), spot


def holdings_tickers() -> list[str]:
    if not (SUPABASE_URL and SERVICE_ROLE_KEY):
        return CORE
    q = f"{SUPABASE_URL}/rest/v1/holdings?select=ticker,tag"
    if HOLDINGS_USER_ID:
        q += f"&user_id=eq.{HOLDINGS_USER_ID}"
    rows = requests.get(q, headers={"apikey": SERVICE_ROLE_KEY, "Authorization": f"Bearer {SERVICE_ROLE_KEY}"}, timeout=30).json()
    held = {
        str(r["ticker"]).upper().strip()
        for r in rows
        if r.get("ticker") and r.get("tag") != "CASH"
    }
    return sorted(held | set(CORE))


def upsert(rows: list[dict]) -> None:
    if not rows:
        print("no IV rows to write"); return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/iv_history",
        params={"on_conflict": "ticker,date"},
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"wrote {len(rows)} IV rows")


def main() -> int:
    if not (SUPABASE_URL and SERVICE_ROLE_KEY):
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"); return 1
    today = dt.date.today().isoformat()
    rows = []
    for t in holdings_tickers():
        try:
            iv, spot = atm_iv(t)
            if iv:
                rows.append({"ticker": t, "date": today, "iv30": round(iv, 4), "spot": spot})
                print(f"  {t:6s} iv30={iv*100:5.1f}%")
            else:
                print(f"  {t:6s} no IV")
        except Exception as e:
            print(f"  {t:6s} error {str(e)[:50]}")
    upsert(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
