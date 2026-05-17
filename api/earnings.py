"""Vercel serverless function for past + upcoming earnings events."""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlparse
from urllib.request import build_opener, HTTPCookieProcessor, Request
import json

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Crumb is reusable across calls on a warm Fluid Compute instance.
_session = {"crumb": None, "opener": None, "fetched_at": 0}


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

    # De-dupe in case the upcoming date appears in quarterly history too.
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
