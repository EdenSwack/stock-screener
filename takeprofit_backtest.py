"""Take-profit rule backtest on the owner's own buys (read-only, prints to the log).

For every USD BUY in ``transactions`` (broad-market / bond / T-bill funds excluded),
follow the stock from the buy date to today and compare, per $1 invested:

  hold   — keep it to today
  rule   — sell when the rule fires, pay 25% tax on the gain, put the rest in SPY
  spy    — buy SPY on the same day instead

All three are valued after a 25% tax on liquidation today (Israeli flat rate; the US
short-term top-up is ignored). Actual sells are ignored — every lot is held to today
unless the rule sells it, so this answers "would the rule have beaten my holding?".

Rules:
  giveback — peak gain ≥ P, then gave back ≥ G of that peak gain, and close < 50-day avg
  trail    — once up ≥ A, sell on a close ≥ T below the highest close since the buy
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("takeprofit")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
USER_ID = os.environ.get("FLEX_USER_ID", "")
TAX = 0.25

# Same list as the app's EtfTrendCheck CORE set.
CORE = {
    "VT", "VTI", "VOO", "SPY", "IVV", "SPYM", "SPLG", "ITOT", "SCHB", "VXUS", "VEA", "VEU", "IEFA", "IXUS", "ACWI",
    "BND", "AGG", "BNDX", "SGOV", "BIL", "SHV", "USFR", "TFLO",
}


def fetch_buys() -> list[dict]:
    headers = {"apikey": SERVICE_ROLE_KEY, "Authorization": f"Bearer {SERVICE_ROLE_KEY}"}
    rows: list[dict] = []
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/transactions",
            params={"user_id": f"eq.{USER_ID}", "type": "eq.BUY", "currency": "eq.USD",
                    "select": "date,ticker,quantity,price", "order": "id", "limit": 1000, "offset": len(rows)},
            headers=headers, timeout=30,
        )
        r.raise_for_status()
        page = r.json()
        rows += page
        if len(page) < 1000:
            break
    return [r for r in rows if r["ticker"] and r["ticker"].upper() not in CORE
            and float(r["quantity"]) > 0 and float(r["price"]) > 0]


def after_tax(v: float, basis: float = 1.0) -> float:
    return v - TAX * max(0.0, v - basis)


def giveback_exit(c: pd.Series, sma50: pd.Series, entry: float, peak_min: float, give: float):
    peak = 0.0
    for d, px in c.items():
        g = px / entry - 1
        peak = max(peak, g)
        if peak >= peak_min and g <= peak * (1 - give) and px < sma50[d]:
            return d
    return None


def trail_exit(c: pd.Series, entry: float, activate: float, trail: float):
    hi = entry
    for d, px in c.items():
        hi = max(hi, px)
        if hi / entry - 1 >= activate and px <= hi * (1 - trail):
            return d
    return None


def main() -> None:
    buys = fetch_buys()
    if not buys:
        log.info("no qualifying buys")
        return
    tickers = sorted({b["ticker"].upper() for b in buys})
    start = min(b["date"] for b in buys)
    log.info("%d buys across %d tickers since %s", len(buys), len(tickers), start)

    yahoo = {t: t.replace(".", "-") for t in tickers}
    raw = yf.download(list(yahoo.values()) + ["SPY"], start=str(date.fromisoformat(start) - timedelta(days=120)),
                      auto_adjust=False, progress=False)["Close"]
    closes = {t: raw[yahoo[t]].dropna() for t in tickers if yahoo[t] in raw}
    spy = raw["SPY"].dropna()
    spy_end = float(spy.iloc[-1])

    # Per lot: price path from the buy, its 50-day average, and the hold / SPY outcomes.
    lots = []
    for b in buys:
        t = b["ticker"].upper()
        c_all = closes.get(t)
        if c_all is None or c_all.empty:
            log.info("  no prices for %s — skipped", t)
            continue
        c = c_all[c_all.index >= pd.Timestamp(b["date"])]
        if c.empty:
            continue
        entry = float(b["price"])
        spy_buy = float(spy[spy.index <= c.index[0]].iloc[-1])
        lots.append({
            "ticker": t, "date": b["date"], "cost": entry * float(b["quantity"]), "entry": entry,
            "c": c, "sma50": c_all.rolling(50, min_periods=20).mean(),
            "hold": after_tax(float(c.iloc[-1]) / entry),
            "spy": after_tax(spy_end / spy_buy),
            "peak_gain": float(c.max()) / entry - 1, "now_gain": float(c.iloc[-1]) / entry - 1,
        })
    total_cost = sum(l["cost"] for l in lots)
    wavg = lambda key: sum(l[key] * l["cost"] for l in lots) / total_cost  # noqa: E731

    log.info("\n%d lots, $%.0f invested", len(lots), total_cost)
    log.info("After-tax value per $1 today (cost-weighted):  hold %.3f   SPY-instead %.3f", wavg("hold"), wavg("spy"))
    behind = [l for l in lots if l["hold"] < l["spy"]]
    log.info("Lots behind SPY-instead: %d of %d ($%.0f of cost)", len(behind), len(lots), sum(l["cost"] for l in behind))
    gave = [l for l in lots if l["peak_gain"] >= 0.20]
    log.info("Lots that were ever up ≥20%%: %d — median peak %.0f%%, median now %.0f%%", len(gave),
             100 * pd.Series([l["peak_gain"] for l in gave]).median() if gave else 0,
             100 * pd.Series([l["now_gain"] for l in gave]).median() if gave else 0)

    def run_rule(name: str, exit_fn) -> list[str]:
        total, fired, fired_detail = 0.0, 0, []
        for l in lots:
            d = exit_fn(l)
            if d is None:
                total += l["hold"] * l["cost"]
                continue
            fired += 1
            v_exit = after_tax(float(l["c"][d]) / l["entry"])
            spy_exit = float(spy[spy.index <= d].iloc[-1])
            v = after_tax(v_exit * spy_end / spy_exit, basis=v_exit)
            total += v * l["cost"]
            fired_detail.append(f"{l['ticker']} {l['date']}→{d.date()} ({v:.3f} vs hold {l['hold']:.3f})")
        log.info("  %-34s fired %3d   value/$1 %.3f   vs hold %+.2f%%", name, fired, total / total_cost,
                 100 * (total / total_cost - wavg("hold")))
        return fired_detail

    log.info("\nGiveback rule (peak ≥ P, gave back ≥ G, below 50-day avg):")
    detail = {}
    for p in (0.15, 0.20, 0.30):
        for g in (0.33, 0.40, 0.50):
            name = f"P={p:.0%} G={g:.0%}"
            detail[name] = run_rule(name, lambda l, p=p, g=g: giveback_exit(l["c"], l["sma50"], l["entry"], p, g))
    log.info("\nTight trail (once up ≥ A, sell T below the high):")
    for a in (0.20, 0.30):
        for tr in (0.10, 0.12, 0.15):
            name = f"A={a:.0%} T={tr:.0%}"
            detail[name] = run_rule(name, lambda l, a=a, tr=tr: trail_exit(l["c"], l["entry"], a, tr))

    for name in ("P=20% G=40%", "A=30% T=12%"):
        log.info("\nSells under %s:", name)
        for line in detail[name]:
            log.info("  %s", line)


if __name__ == "__main__":
    main()
