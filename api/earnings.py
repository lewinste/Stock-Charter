"""Vercel serverless function for past + upcoming earnings events."""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlparse
from urllib.request import build_opener, HTTPCookieProcessor, Request, urlopen
import json

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# SEC's rate-limit policy requires a contact-email-style UA. They 403 anything else.
SEC_UA = "Stock-Charter ori.lewinstein@gmail.com"

# Crumb is reusable across calls on a warm Fluid Compute instance.
_session = {"crumb": None, "opener": None, "fetched_at": 0}
# ticker -> zero-padded CIK string; populated lazily and reused across requests.
_cik_cache = {"map": None, "fetched_at": 0}


def _get_session():
    """Return (opener, crumb), refreshing once per hour."""
    now = int(datetime.now(timezone.utc).timestamp())
    if _session["crumb"] and now - _session["fetched_at"] < 3600:
        return _session["opener"], _session["crumb"]

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    # Prime cookies; the host occasionally 404s but still sets the A3 cookie.
    try:
        opener.open(Request("https://fc.yahoo.com", headers={"User-Agent": UA}), timeout=8).read()
    except Exception:
        pass

    crumb = opener.open(
        Request("https://query2.finance.yahoo.com/v1/test/getcrumb", headers={"User-Agent": UA}),
        timeout=8,
    ).read().decode().strip()

    _session.update(opener=opener, crumb=crumb, fetched_at=now)
    return opener, crumb


def _fmt_date(unix_ts):
    return datetime.utcfromtimestamp(int(unix_ts)).strftime("%Y-%m-%d")


def _get_cik(ticker):
    """Resolve a ticker to a zero-padded SEC CIK string, or None if unknown."""
    now = int(datetime.now(timezone.utc).timestamp())
    # Refresh the lookup table daily — SEC adds/removes tickers regularly.
    if not _cik_cache["map"] or now - _cik_cache["fetched_at"] > 86400:
        try:
            req = Request(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": SEC_UA},
            )
            raw = json.loads(urlopen(req, timeout=10).read())
            mapping = {}
            for v in raw.values():
                tk = (v.get("ticker") or "").upper()
                if tk:
                    mapping[tk] = str(v["cik_str"]).zfill(10)
            _cik_cache.update(map=mapping, fetched_at=now)
        except Exception:
            if not _cik_cache["map"]:
                _cik_cache["map"] = {}
    return _cik_cache["map"].get(ticker.upper())


def fetch_sec_announcement_dates(ticker):
    """Pull historical earnings-announcement dates from SEC EDGAR 8-K filings.

    Item 2.02 ("Results of Operations and Financial Condition") is filed on the
    same day a company puts out its earnings press release, so the filingDate
    matches Yahoo's reportedDate exactly. Returns [] for non-US tickers that
    SEC doesn't know about.
    """
    cik = _get_cik(ticker)
    if not cik:
        return []
    try:
        req = Request(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": SEC_UA},
        )
        sub = json.loads(urlopen(req, timeout=10).read())
    except Exception:
        return []

    recent = sub.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    items = recent.get("items", []) or [""] * len(forms)

    raw_dates = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        item_str = items[i] if i < len(items) else ""
        if "2.02" not in (item_str or ""):
            continue
        if i < len(dates):
            raw_dates.append(dates[i])

    # Some companies (notably TSLA) file 8-K item 2.02 for production/delivery
    # updates ~2-3 weeks before the actual earnings release. Earnings happen at
    # most once per quarter, so within any ~60-day window keep only the latest
    # filing — that's the actual earnings, which always lands after the prelim.
    raw_dates.sort(reverse=True)
    out = []
    last_kept = None
    for d in raw_dates:
        if last_kept is None:
            out.append(d)
            last_kept = d
            continue
        try:
            gap = (datetime.strptime(last_kept, "%Y-%m-%d")
                   - datetime.strptime(d, "%Y-%m-%d")).days
        except Exception:
            continue
        if gap >= 60:
            out.append(d)
            last_kept = d
    return out


def _raw(obj, key):
    v = obj.get(key)
    if isinstance(v, dict):
        return v.get("raw")
    return v


def fetch_earnings(ticker):
    opener, crumb = _get_session()
    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        f"?modules=earnings,calendarEvents&crumb={crumb}"
    )
    resp = opener.open(Request(url, headers={"User-Agent": UA}), timeout=12)
    data = json.loads(resp.read().decode("utf-8"))

    result = (data.get("quoteSummary", {}) or {}).get("result") or []
    if not result:
        return []

    r = result[0]
    earnings_chart = (r.get("earnings", {}) or {}).get("earningsChart", {}) or {}
    calendar_earnings = ((r.get("calendarEvents", {}) or {}).get("earnings", {}) or {})

    events = []

    # Past quarters: reportedDate carries the actual announcement date.
    for q in earnings_chart.get("quarterly", []) or []:
        reported = q.get("reportedDate") or {}
        ts = reported.get("raw")
        if not ts:
            continue
        events.append({
            "date": _fmt_date(ts),
            "type": "past",
            "epsActual": _raw(q, "actual"),
            "epsEstimate": _raw(q, "estimate"),
            "surprisePct": float(q["surprisePct"]) if q.get("surprisePct") not in (None, "") else None,
            "quarter": q.get("date"),
        })

    # Upcoming: prefer earningsChart.earningsDate, fall back to calendarEvents.
    upcoming = None
    chart_dates = earnings_chart.get("earningsDate") or []
    if chart_dates:
        upcoming = chart_dates[0].get("raw")
    if not upcoming:
        cal_dates = calendar_earnings.get("earningsDate") or []
        if cal_dates:
            upcoming = cal_dates[0].get("raw")

    if upcoming:
        events.append({
            "date": _fmt_date(upcoming),
            "type": "upcoming",
            "epsActual": None,
            "epsEstimate": _raw(calendar_earnings, "earningsAverage"),
            "surprisePct": None,
            "isEstimate": bool(calendar_earnings.get("isEarningsDateEstimate")
                               or earnings_chart.get("isEarningsDateEstimate")),
        })

    # Backfill older quarters from SEC EDGAR 8-K filings (item 2.02 = earnings
    # press release). These don't carry EPS values, but they give us accurate
    # historical announcement dates well beyond Yahoo's 4-quarter window.
    yahoo_dates = {e["date"] for e in events}
    for d in fetch_sec_announcement_dates(ticker):
        if d in yahoo_dates:
            continue
        events.append({
            "date": d,
            "type": "past",
            "epsActual": None,
            "epsEstimate": None,
            "surprisePct": None,
            "source": "sec",
        })

    # De-dupe + sort ascending.
    seen = set()
    unique = []
    for e in sorted(events, key=lambda x: x["date"]):
        if e["date"] in seen:
            continue
        seen.add(e["date"])
        unique.append(e)
    return unique


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        ticker = params.get("ticker", ["AAPL"])[0].upper()

        try:
            events = fetch_earnings(ticker)
        except Exception as e:
            self._send_json({"error": f"Failed to fetch earnings for {ticker}: {e}"}, 502)
            return

        self._send_json({"ticker": ticker, "events": events})

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=3600, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
