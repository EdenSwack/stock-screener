"""Pull the owner's IBKR positions + cash via the Flex Web Service and cache them
in Supabase for the portfolio app to read.

Why here and not the Cloudflare Worker: IBKR's edge returns "403 Access Denied"
to Cloudflare Workers (datacenter-IP / TLS fingerprint). A normal client like a
GitHub Actions runner is allowed, so the fetch lives in this repo's cron and
writes the result to ``broker_flex_cache``; the Worker's /flex-sync then just
reads that row. Ends the recurring SnapTrade reconnect requirement.

Two-step Flex flow: SendRequest returns a reference code, GetStatement returns
the statement (polling while it generates). Emits the SAME row shape the app's
sync path expects: positions + one "<CUR> Cash" row per currency.

Also appends new dividends, fees, deposits/withdrawals and stock trades from the same
statement to the ``transactions`` log (see insert_transactions for the dedup rules).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("flex_sync")

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
FLEX_USER_ID = os.environ.get("FLEX_USER_ID", "")


def _num(v: str | None) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# Mirrors TICKER_ALIAS in the app's Transactions page (renamed tickers).
TICKER_ALIAS = {"SPLG": "SPYM"}


def _norm_ticker(sym: str | None) -> str:
    # Class-share tickers arrive with a space ("BRK B"); Finnhub/Yahoo want a dot.
    return (sym or "").strip().upper().replace(" ", ".")


def _tx_ticker(sym: str | None) -> str | None:
    t = _norm_ticker(sym)
    return TICKER_ALIAS.get(t, t) or None


def _date(v: str | None) -> str | None:
    # Flex dates come as 20261015, 20261015;093000 or 2026-10-15 depending on the
    # query's date format — normalize to YYYY-MM-DD.
    digits = re.sub(r"\D", "", (v or "").split(";")[0].split(",")[0].split(" ")[0])
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) >= 8 else None


def send_request() -> tuple[str, str]:
    r = requests.get(f"{FLEX_BASE}/SendRequest", params={"t": TOKEN, "q": QUERY_ID, "v": 3}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.findtext("Status") != "Success":
        raise RuntimeError(f"SendRequest failed ({root.findtext('ErrorCode')}): {root.findtext('ErrorMessage')}")
    ref = root.findtext("ReferenceCode")
    # Ignore the <Url> IBKR returns — it can point at the legacy `gdcdyn` host,
    # which no longer resolves from the GitHub runner. Always call GetStatement on
    # the known-good `ndcdyn` base (FLEX_BASE).
    base = f"{FLEX_BASE}/GetStatement"
    return ref, base


def get_statement(ref: str, base: str) -> str:
    # Statement generation is usually near-instant but can lag; poll on the "in
    # progress" code (1019) until the real FlexQueryResponse comes back.
    for _ in range(10):
        r = requests.get(base, params={"t": TOKEN, "q": ref, "v": 3}, timeout=60)
        r.raise_for_status()
        text = r.text
        if "<FlexQueryResponse" in text:
            return text
        root = ET.fromstring(text)
        if root.findtext("ErrorCode") == "1019":
            time.sleep(3)
            continue
        raise RuntimeError(f"GetStatement failed: {root.findtext('ErrorMessage')}")
    raise RuntimeError("statement not ready after polling")


def parse(xml_text: str) -> tuple[list[dict], str | None]:
    root = ET.fromstring(xml_text)
    out: list[dict] = []

    for op in root.iter("OpenPosition"):
        a = op.attrib
        if a.get("assetCategory") and a["assetCategory"] != "STK":
            continue  # stocks/ETFs only
        qty = _num(a.get("position"))
        if qty == 0:
            continue
        out.append({
            "ticker": _norm_ticker(a.get("symbol")),
            "num_shares": qty,
            "current_price": _num(a.get("markPrice")),
            "avg_buy_price": _num(a.get("costBasisPrice")),
            "currency": a.get("currency") or "USD",
            "platform": "Interactive Brokers",
        })

    # endingCash is trade-date (purchases already deducted), matching the trade-date
    # positions above — endingSettledCash would double-count unsettled buys. Skip the
    # BASE_SUMMARY row (levelOfDetail=BaseCurrency); emit one row per currency.
    for cr in root.iter("CashReportCurrency"):
        a = cr.attrib
        if a.get("levelOfDetail") != "Currency":
            continue
        cash = _num(a.get("endingCash"))
        cur = a.get("currency") or "USD"
        out.append({
            "ticker": f"{cur} Cash",
            "num_shares": 1,
            "current_price": cash,
            "avg_buy_price": cash,
            "currency": cur,
            "platform": "Interactive Brokers",
        })

    stmt = root.find(".//FlexStatement")
    ts = stmt.get("whenGenerated") if stmt is not None else None
    return out, ts


# Flex CashTransaction type -> the app's transaction type. Credit interest has no
# matching type in the app (its CSV import skips it too), so it's left out.
CASH_TYPES = {
    "Dividends": "DIVIDEND",
    "Payment In Lieu Of Dividends": "DIVIDEND",
    "Withholding Tax": "FEE",
    "Broker Interest Paid": "FEE",
    "Other Fees": "FEE",
    "Deposits/Withdrawals": None,  # sign decides DEPOSIT vs WITHDRAW
}


def parse_transactions(xml_text: str) -> list[dict]:
    """Cash transactions + stock trades, in the transactions-table shape the app's
    IBKR CSV import writes (amount in price, quantity 1; absolute values)."""
    root = ET.fromstring(xml_text)
    out: list[dict] = []

    for ct in root.iter("CashTransaction"):
        a = ct.attrib
        if a.get("levelOfDetail", "DETAIL").upper() != "DETAIL":
            continue
        kind = a.get("type", "")
        if kind not in CASH_TYPES:
            continue
        amount = _num(a.get("amount"))
        d = _date(a.get("dateTime") or a.get("settleDate") or a.get("reportDate"))
        cur = a.get("currency") or "USD"
        if not amount or not d:
            continue
        if cur not in ("USD", "ILS"):
            log.warning("skipping %s %s %s %s — currency not supported by the app", d, kind, a.get("symbol"), cur)
            continue
        tx_type = CASH_TYPES[kind] or ("DEPOSIT" if amount > 0 else "WITHDRAW")
        out.append({
            "date": d, "type": tx_type, "ticker": _tx_ticker(a.get("symbol")),
            "quantity": 1, "price": abs(amount), "currency": cur,
            "platform": "Interactive Brokers", "notes": a.get("description") or None,
        })

    for tr in root.iter("Trade"):
        a = tr.attrib
        if a.get("assetCategory") and a["assetCategory"] != "STK":
            continue
        if a.get("levelOfDetail", "EXECUTION").upper() != "EXECUTION":
            continue
        side = (a.get("buySell") or "").upper()
        qty, price = abs(_num(a.get("quantity"))), abs(_num(a.get("tradePrice")))
        d = _date(a.get("tradeDate") or a.get("dateTime"))
        cur = a.get("currency") or "USD"
        if side not in ("BUY", "SELL") or not qty or not price or not d or cur not in ("USD", "ILS"):
            continue
        out.append({
            "date": d, "type": side, "ticker": _tx_ticker(a.get("symbol")),
            "quantity": qty, "price": price, "currency": cur,
            "platform": "Interactive Brokers", "notes": a.get("description") or None,
        })
    return out


def _sb_headers() -> dict:
    return {"apikey": SERVICE_ROLE_KEY, "Authorization": f"Bearer {SERVICE_ROLE_KEY}"}


def _tx_key(t: dict) -> str:
    # Same key as txKey() on the app's Transactions page.
    return f"{t['date']}|{t['type']}|{t['ticker'] or ''}|{float(t['quantity']):.4f}|{float(t['price']):.4f}"


def insert_transactions(txs: list[dict]) -> None:
    """Append Flex transactions the log doesn't already have.

    The log was seeded by manual IBKR CSV imports, whose dates can differ from Flex's
    by a day or two (pay vs settle date), so:
      - cash rows are skipped when the same type/ticker/amount exists within 5 days;
      - trades are only added after the latest IBKR trade already in the log, since
        the CSV may group fills differently and a duplicate BUY would corrupt cost basis.
    """
    if not SUPABASE_URL or not SERVICE_ROLE_KEY or not FLEX_USER_ID:
        log.info("SUPABASE_* / FLEX_USER_ID not set — skipping transactions (parsed %d)", len(txs))
        return
    existing: list[dict] = []
    page = 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/transactions",
            params={"user_id": f"eq.{FLEX_USER_ID}", "select": "date,type,ticker,quantity,price,platform",
                    "order": "id", "limit": page, "offset": len(existing)},
            headers=_sb_headers(), timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        existing += rows
        if len(rows) < page:
            break

    keys = {_tx_key(t) for t in existing}
    near: dict[tuple, list[date]] = {}
    for t in existing:
        if t["type"] in ("BUY", "SELL"):
            continue
        near.setdefault((t["type"], t["ticker"] or "", round(float(t["price"]), 2)), []).append(date.fromisoformat(t["date"]))
    ib_trade_dates = [t["date"] for t in existing
                      if t["type"] in ("BUY", "SELL") and (t.get("platform") or "") == "Interactive Brokers"]
    trade_cutoff = max(ib_trade_dates) if ib_trade_dates else None

    new: list[dict] = []
    for t in txs:
        k = _tx_key(t)
        if k in keys:
            continue
        if t["type"] in ("BUY", "SELL"):
            if trade_cutoff and t["date"] <= trade_cutoff:
                continue
        else:
            d = date.fromisoformat(t["date"])
            nk = (t["type"], t["ticker"] or "", round(t["price"], 2))
            if any(abs((d - e).days) <= 5 for e in near.get(nk, [])):
                continue
            near.setdefault(nk, []).append(d)
        keys.add(k)
        new.append({**t, "user_id": FLEX_USER_ID})

    if new:
        resp = requests.post(f"{SUPABASE_URL}/rest/v1/transactions",
                             headers={**_sb_headers(), "Content-Type": "application/json"},
                             json=new, timeout=30)
        resp.raise_for_status()
    log.info("transactions: parsed %d from Flex, inserted %d new (trade cutoff %s)", len(txs), len(new), trade_cutoff)


def upsert(positions: list[dict], statement_ts: str | None) -> None:
    if not SUPABASE_URL or not SERVICE_ROLE_KEY or not FLEX_USER_ID:
        log.info("SUPABASE_* / FLEX_USER_ID not set — skipping publish (fetched %d rows)", len(positions))
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/broker_flex_cache",
        params={"on_conflict": "user_id"},
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        json={
            "user_id": FLEX_USER_ID,
            "positions": positions,
            "statement_ts": statement_ts,
            "updated_at": "now()",
        },
        timeout=30,
    )
    resp.raise_for_status()
    log.info("published %d rows to broker_flex_cache (statement %s)", len(positions), statement_ts)


def main() -> int:
    if not TOKEN or not QUERY_ID:
        log.error("IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set")
        return 1
    ref, base = send_request()
    xml_text = get_statement(ref, base)
    positions, statement_ts = parse(xml_text)
    if not positions:
        log.error("parsed 0 positions — refusing to overwrite cache with an empty snapshot")
        return 1
    upsert(positions, statement_ts)
    # Positions are the critical path; a transaction-import failure is logged, not fatal.
    try:
        insert_transactions(parse_transactions(xml_text))
    except Exception:
        log.exception("transaction import failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
