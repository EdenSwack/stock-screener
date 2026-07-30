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
"""

from __future__ import annotations

import logging
import os
import sys
import time
import xml.etree.ElementTree as ET

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


def _norm_ticker(sym: str | None) -> str:
    # Class-share tickers arrive with a space ("BRK B"); Finnhub/Yahoo want a dot.
    return (sym or "").strip().upper().replace(" ", ".")


def send_request() -> tuple[str, str]:
    r = requests.get(f"{FLEX_BASE}/SendRequest", params={"t": TOKEN, "q": QUERY_ID, "v": 3}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.findtext("Status") != "Success":
        raise RuntimeError(f"SendRequest failed ({root.findtext('ErrorCode')}): {root.findtext('ErrorMessage')}")
    ref = root.findtext("ReferenceCode")
    base = root.findtext("Url") or f"{FLEX_BASE}/GetStatement"
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
