"""Vercel serverless function for real-time quote data."""

from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request


def fetch_quote(ticker):
    """Fetch the latest intraday candle from Yahoo Finance (1d range, 1m interval)."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=1d&interval=1m"
    )
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    resp = urlopen(req, timeout=10)
    data = json.loads(resp.read().decode("utf-8"))

    result = data["chart"]["result"]
    if not result:
        return None

    chart = result[0]
    meta = chart["meta"]
    timestamps = chart.get("timestamp", [])
    quote = chart["indicators"]["quote"][0]

    # Get current trading state from meta
    market_state = meta.get("currentTradingPeriod", {})
    regular = market_state.get("regular", {})

    # Build the current day's aggregated OHLCV from minute bars
    if not timestamps:
        return {
            "ticker": meta.get("symbol", ticker),
            "price": round(meta.get("regularMarketPrice", 0), 2),
            "previousClose": round(meta.get("chartPreviousClose", 0), 2),
            "open": None,
            "high": None,
            "low": None,
            "close": round(meta.get("regularMarketPrice", 0), 2),
            "volume": 0,
            "trading": False,
        }

    # Aggregate minute bars into a single daily candle
    day_open = None
    day_high = float('-inf')
    day_low = float('inf')
    day_close = None
    day_volume = 0

    for i in range(len(timestamps)):
        o = quote["open"][i]
        h = quote["high"][i]
        l = quote["low"][i]
        c = quote["close"][i]
        v = quote["volume"][i]
        if any(x is None for x in [o, h, l, c]):
            continue
        if day_open is None:
            day_open = float(o)
        day_high = max(day_high, float(h))
        day_low = min(day_low, float(l))
        day_close = float(c)
        day_volume += int(v) if v else 0

    # Determine if market is currently open
    exchange_tz = meta.get("exchangeTimezoneName", "")
    trading = meta.get("currentTradingPeriod", {}).get("regular", {})
    now_ts = timestamps[-1] if timestamps else 0
    is_trading = (
        trading.get("start", 0) <= now_ts <= trading.get("end", 0)
        if trading else False
    )

    return {
        "ticker": meta.get("symbol", ticker),
        "name": meta.get("longName") or meta.get("shortName", ""),
        "exchange": meta.get("exchangeName", ""),
        "price": round(meta.get("regularMarketPrice", 0), 2),
        "previousClose": round(meta.get("chartPreviousClose", 0), 2),
        "open": round(day_open, 2) if day_open else None,
        "high": round(day_high, 2) if day_high != float('-inf') else None,
        "low": round(day_low, 2) if day_low != float('inf') else None,
        "close": round(day_close, 2) if day_close else None,
        "volume": day_volume,
        "trading": is_trading,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        ticker = params.get("ticker", ["AAPL"])[0].upper()

        try:
            quote = fetch_quote(ticker)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        if not quote:
            self._send_json({"error": f"No data for {ticker}"}, 404)
            return

        self._send_json(quote)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=5, stale-while-revalidate=10")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
