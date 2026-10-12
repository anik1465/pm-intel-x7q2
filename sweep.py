#!/usr/bin/env python3
"""
Pre-Market Intel — cloud sweep (deterministic, no LLM).

Runs on GitHub Actions weekdays ~5:47 AM PT. Sweeps open financial
sources for pre-market most-active stocks, major headlines, futures
snapshot and the day's earnings, then rewrites briefing-data.js in the
repo root (consumed by the static site served from the same repo).

Never fabricates: every item comes from a fetched page; gaps go to notes.
"""
import json
import re
import sys
import time
import html as _html
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
OUT_FILE = "briefing-data.js"
notes = []


def get(url, timeout=20, **kw):
    r = requests.get(url, headers=UA, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def pt_now():
    pt = timezone(timedelta(hours=-7), name="PDT")  # Actions runs in UTC; PT offset fixed below
    now = datetime.now(timezone.utc)
    # crude DST: US PDT Mar second Sunday .. Nov first Sunday
    y = now.year
    mar8 = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    off = -7 if dst_start <= now < dst_end else -8
    return now.astimezone(timezone(timedelta(hours=off)))


# ---------------------------------------------------------------- movers
def fetch_spark(ticker, days=5):
    """Last N daily closes (oldest→newest) from stockanalysis history page."""
    try:
        url = f"https://stockanalysis.com/stocks/{ticker.lower()}/history/"
        soup = BeautifulSoup(get(url, timeout=15).text, "html.parser")
        table = soup.find("table")
        if not table:
            return []
        closes = []
        for tr in table.find_all("tr")[1:days + 1]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 5:
                try:
                    closes.append(float(tds[4].replace(",", "").replace("$", "")))
                except ValueError:
                    pass
        return closes[::-1]
    except Exception:
        return []


def fetch_movers():
    """stockanalysis.com premarket movers; falls back to regular-session lists."""
    urls = [
        ("https://stockanalysis.com/markets/premarket/", "premarket"),
        ("https://stockanalysis.com/markets/active/", "most active (regular session)"),
    ]
    for url, label in urls:
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
            table = soup.find("table")
            if not table:
                continue
            heads = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            rows = []
            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 4:
                    continue
                rows.append(tds)
            if not rows:
                continue
            movers = []
            for tds in rows:
                rec = dict(zip(heads, tds))
                sym = (rec.get("symbol") or rec.get("") or "").upper().strip()
                if not sym or not re.fullmatch(r"[A-Z.\-]{1,6}", sym):
                    continue
                chg = rec.get("% change") or rec.get("change") or ""
                price = (rec.get("premkt. price") or rec.get("price") or rec.get("last") or "")
                vol = (rec.get("pre. volume") or rec.get("volume") or rec.get("vol") or "")
                movers.append({
                    "ticker": sym,
                    "price": price.replace("$", ""),
                    "chg": chg if chg.startswith(("+", "-")) else ("+" + chg if chg and not chg.startswith("-") else chg),
                    "volume": vol,
                    "catalyst": "",
                    "source": "stockanalysis",
                    "url": "https://stockanalysis.com/stocks/" + sym.lower() + "/",
                })
                if len(movers) >= 12:
                    break
            if movers:
                for m in movers:
                    m["spark"] = fetch_spark(m["ticker"])
                    time.sleep(0.3)
                if label != "premarket":
                    notes.append(f"Premarket table unavailable; movers reflect {label}.")
                return movers
        except Exception as e:
            notes.append(f"movers source failed ({url.split('/')[2]}): {type(e).__name__}")
    return []


# ---------------------------------------------------------------- headlines
FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Benzinga", "https://www.benzinga.com/feed"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]
TAG_RULES = [
    (r"\b(fed|fomc|rate|inflation|cpi|powell|treasury|yield)\b", "FED"),
    (r"\b(earn|q[1-4]\b|profit|revenue|guidance|eps)\b", "EARNINGS"),
    (r"\b(nvidia|apple|microsoft|amazon|meta|alphabet|google|tesla|ai\b|chip|semiconductor)\b", "TECH"),
    (r"\b(oil|crude|opec|energy|gold)\b", "ENERGY"),
    (r"\b(china|russia|ukraine|iran|israel|tariff|war|sanction|houthis)\b", "GEOPOLITICS"),
    (r"\b(bitcoin|crypto|ethereum)\b", "CRYPTO"),
    (r"\b(ipo|merger|acquisition|deal|buyback)\b", "DEALS"),
]


def tag_of(title):
    t = title.lower()
    for pat, tag in TAG_RULES:
        if re.search(pat, t):
            return tag
    return "MARKETS"


def fetch_headlines(limit=15):
    items = []
    now = datetime.now(timezone.utc)
    for source, url in FEEDS:
        try:
            root = ET.fromstring(get(url, timeout=15).content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue
                age_h = None
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    age_h = (now - dt.astimezone(timezone.utc)).total_seconds() / 3600
                except Exception:
                    pass
                if age_h is not None and age_h > 36:
                    continue
                items.append({
                    "time": _fmt_pub(pub),
                    "source": source,
                    "title": _html.unescape(title),
                    "url": link,
                    "tag": tag_of(title),
                    "_age": age_h if age_h is not None else 99,
                })
        except Exception as e:
            notes.append(f"feed failed ({source}): {type(e).__name__}")
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["_age"]):
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        it.pop("_age", None)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _fmt_pub(pub):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub)
        et = dt.astimezone(timezone(timedelta(hours=-4)))  # label ET; exact DST not critical for display
        return et.strftime("%a %-I:%M %p ET")
    except Exception:
        return pub[:16]


# ---------------------------------------------------------------- futures
CNBC_SYMBOLS = {
    "@ES.1": "S&P FUT", "@NQ.1": "NASDAQ FUT", "@YM.1": "DOW FUT",
    ".VIX": "VIX", "US10Y": "US 10Y", "@CL.1": "WTI CRUDE",
    "@GC.1": "GOLD", ".DXY": "DXY", "BTC.CM=": "BITCOIN",
}


def fetch_futures():
    try:
        syms = "|".join(CNBC_SYMBOLS)
        url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/"
               f"symbolType/symbol?symbols={syms}&output=json")
        data = get(url, timeout=15).json()
        out = []
        for q in data.get("FormattedQuoteResult", {}).get("FormattedQuote", []):
            code = q.get("symbol") or ""
            label = CNBC_SYMBOLS.get(code)
            if not label:
                continue
            last = q.get("last") or ""
            chg = q.get("change_pct") or q.get("changePercentage") or ""
            chg = str(chg).replace("%", "").strip()
            if chg and not chg.startswith(("+", "-")):
                chg = "+" + chg
            out.append({"label": label, "value": str(last), "chg": (chg + "%") if chg else ""})
        out = [f for f in out if f["value"]]
        if not any(f["label"] == "S&P FUT" for f in out):
            notes.append("index futures quotes unavailable from the cloud feed this run")
        return out
    except Exception as e:
        notes.append(f"futures snapshot unavailable: {type(e).__name__}")
        return []


# ---------------------------------------------------------------- earnings
def fetch_earnings(pt):
    date = pt.strftime("%Y-%m-%d")
    try:
        data = get(f"https://api.nasdaq.com/api/calendar/earnings?date={date}",
                   timeout=15).json()
        rows = (data.get("data") or {}).get("rows") or []
        def cap_num(r):
            s = (r.get("marketCap") or "").replace("$", "").replace(",", "").strip()
            mult = 1e9 if "B" in s else (1e6 if "M" in s else 1)
            m = re.search(r"[\d.]+", s)
            return float(m.group()) * mult if m else 0
        rows = sorted(rows, key=cap_num, reverse=True)[:8]
        out = []
        for r in rows:
            sym = (r.get("symbol") or "").strip()
            when = (r.get("time") or "").upper()
            when = "BMO" if "PRE" in when or "BMO" in when else ("AMC" if "AFTER" in when or "AMC" in when else when or "TODAY")
            out.append({"ticker": sym, "when": when, "note": (r.get("companyName") or "")[:40]})
        return out
    except Exception as e:
        notes.append(f"earnings calendar unavailable: {type(e).__name__}")
        return []


# ---------------------------------------------------------------- desk view (mechanical)
def desk_view(movers, headlines, futures, earnings):
    out = []
    es = next((f for f in futures if f["label"] == "S&P FUT"), None)
    if es:
        tone = "green" if es["chg"].startswith("+") else ("red" if es["chg"].startswith("-") else "flat")
        out.append(f"Futures {tone} at sweep time (S&P {es['chg']}, Nasdaq "
                   f"{next((f['chg'] for f in futures if f['label'] == 'NASDAQ FUT'), 'n/a')}). "
                   f"Confirm direction again at 6:15 — pre-market flips fast.")
    else:
        vix = next((f for f in futures if f["label"] == "VIX"), None)
        ten = next((f for f in futures if f["label"] == "US 10Y"), None)
        if vix or ten:
            out.append(f"Risk snapshot at sweep: VIX {vix['value'] if vix else 'n/a'}, "
                       f"10Y {ten['value'] if ten else 'n/a'}. Index futures feed unavailable — "
                       f"verify direction on your broker screen at 6:15.")
    if movers:
        top = movers[0]
        out.append(f"Highest-profile mover: {top['ticker']} {top['chg']} on {top['volume'] or 'heavy'} volume — "
                   f"check its news catalyst before touching it.")
    bmo = [e["ticker"] for e in earnings if e["when"] == "BMO"]
    if bmo:
        out.append(f"Reporting before the open: {', '.join(bmo[:5])} — expect gap moves and sympathy reads in their sectors.")
    geo = [h for h in headlines if h["tag"] == "GEOPOLITICS"]
    if geo:
        out.append(f"Overnight risk headline: {geo[0]['title'][:110]}")
    return out[:5]


# ---------------------------------------------------------------- main
def already_fresh(pt):
    """Skip when today's briefing already exists (local AI agent published first)."""
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'"generatedAtPT":\s*"([^"]+)"', head)
        if m:
            existing = datetime.strptime(m.group(1).split("·")[0].strip(), "%b %d, %Y")
            return existing.date() == pt.date()
    except Exception:
        pass
    return False


def main():
    pt = pt_now()
    if already_fresh(pt):
        print("briefing already generated today (agent edition present) — skipping")
        return
    payload = {
        "generatedAtPT": pt.strftime("%b %-d, %Y · %-I:%M %p PT"),
        "tradingDate": pt.strftime("%a %b %-d"),
        "futures": fetch_futures(),
        "movers": fetch_movers(),
        "headlines": fetch_headlines(),
        "earnings": fetch_earnings(pt),
        "watch": [],
        "notes": "",
    }
    payload["watch"] = desk_view(payload["movers"], payload["headlines"],
                                 payload["futures"], payload["earnings"])
    base = "Cloud sweep (deterministic — no AI analysis this run)."
    payload["notes"] = (base + (" " + " ".join(notes) if notes else "")).strip()

    js = "window.BRIEFING = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(js)
    print(f"wrote {OUT_FILE}: {len(payload['movers'])} movers, "
          f"{len(payload['headlines'])} headlines, {len(payload['futures'])} futures, "
          f"{len(payload['earnings'])} earnings")


if __name__ == "__main__":
    sys.exit(main())
