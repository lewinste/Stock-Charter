"""Vercel serverless function for stock chart data API."""

from http.server import BaseHTTPRequestHandler
import json
import traceback
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request
from datetime import datetime, timedelta


# Period to days mapping
PERIOD_DAYS = {
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "5y": 1825,
}


def fetch_yahoo_data(ticker, period, start_ts=None, end_ts=None):
    """Fetch OHLCV data from Yahoo Finance chart JSON API.

    If start_ts/end_ts are provided (unix timestamps), use them directly.
    Otherwise fall back to the named period.
    """
    if start_ts is not None:
        start = int(start_ts)
        now = int(end_ts) if end_ts else int(datetime.now().timestamp())
    else:
        days = PERIOD_DAYS.get(period, 365)
        now = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=days)).timestamp())

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={start}&period2={now}&interval=1d"
    )
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    resp = urlopen(req, timeout=15)
    data = json.loads(resp.read().decode("utf-8"))

    result = data["chart"]["result"]
    if not result:
        return [], {}

    chart = result[0]
    meta = chart.get("meta", {})
    timestamps = chart["timestamp"]
    quote = chart["indicators"]["quote"][0]

    rows = []
    for i in range(len(timestamps)):
        o = quote["open"][i]
        h = quote["high"][i]
        l = quote["low"][i]
        c = quote["close"][i]
        v = quote["volume"][i]

        if any(x is None for x in [o, h, l, c, v]):
            continue

        dt = datetime.utcfromtimestamp(timestamps[i]).strftime("%Y-%m-%d")
        rows.append({
            "date": dt,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": int(v),
        })
    return rows, meta


def compute_rsi(closes, period=14):
    """Compute RSI from a list of close prices."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    rsi = [None] * len(closes)
    gains = []
    losses = []

    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(closes)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period

        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = round(100 - (100 / (1 + rs)), 2)

    return rsi


def compute_atr(highs, lows, closes, period=14):
    """Compute ATR from lists."""
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    atr = [None] * len(closes)
    if len(trs) < period:
        return atr

    atr_val = sum(trs[:period]) / period
    atr[period - 1] = atr_val
    for i in range(period, len(closes)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        atr[i] = atr_val
    return atr


def compute_supertrend(highs, lows, closes, period=10, multiplier=3.0):
    """Compute SuperTrend from lists."""
    n = len(closes)
    atr = compute_atr(highs, lows, closes, period)

    upper_band = [0.0] * n
    lower_band = [0.0] * n
    supertrend = [None] * n
    direction = [0] * n

    for i in range(n):
        hl2 = (highs[i] + lows[i]) / 2
        if atr[i] is not None:
            upper_band[i] = hl2 + multiplier * atr[i]
            lower_band[i] = hl2 - multiplier * atr[i]
        else:
            upper_band[i] = hl2
            lower_band[i] = hl2

    direction[0] = -1
    supertrend[0] = upper_band[0]

    for i in range(1, n):
        if closes[i] > upper_band[i - 1]:
            direction[i] = 1
        elif closes[i] < lower_band[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
            if direction[i] == 1 and lower_band[i] < lower_band[i - 1]:
                lower_band[i] = lower_band[i - 1]
            if direction[i] == -1 and upper_band[i] > upper_band[i - 1]:
                upper_band[i] = upper_band[i - 1]

        supertrend[i] = lower_band[i] if direction[i] == 1 else upper_band[i]

    return supertrend, direction


def compute_cmf(highs, lows, closes, volumes, period=20):
    """Chaikin Money Flow: sum(MFV, n) / sum(Volume, n)"""
    n = len(closes)
    cmf = [None] * n
    mfv = []
    for i in range(n):
        hl = highs[i] - lows[i]
        if hl == 0:
            mfv.append(0.0)
        else:
            mfv.append(((closes[i] - lows[i]) - (highs[i] - closes[i])) / hl * volumes[i])
    for i in range(period - 1, n):
        vol_sum = sum(volumes[i - period + 1: i + 1])
        if vol_sum == 0:
            cmf[i] = 0.0
        else:
            cmf[i] = round(sum(mfv[i - period + 1: i + 1]) / vol_sum, 4)
    return cmf


def compute_macd_v(closes, highs, lows, fast=12, slow=26, signal=9, atr_len=26):
    """Compute volatility-normalised MACD (Spiroglou 2022).
    MACD-V = ((EMA(fast) - EMA(slow)) / ATR(atr_len)) * 100
    """
    n = len(closes)
    fast_ema = compute_ema_from_values(closes, fast)
    slow_ema = compute_ema_from_values(closes, slow)
    atr_vals = compute_atr(highs, lows, closes, atr_len)
    macdv = [None] * n
    for i in range(n):
        if (fast_ema[i] is not None and slow_ema[i] is not None
                and atr_vals[i] is not None and atr_vals[i] != 0):
            macdv[i] = (fast_ema[i] - slow_ema[i]) / atr_vals[i] * 100
    sig = compute_ema_from_values(macdv, signal)
    hist = [None] * n
    for i in range(n):
        if macdv[i] is not None and sig[i] is not None:
            hist[i] = macdv[i] - sig[i]
    return macdv, sig, hist


def compute_macd(closes, fast=12, slow=26, signal=9):
    """Compute MACD line, signal line, and histogram from close prices."""
    n = len(closes)
    fast_ema = compute_ema_from_values(closes, fast)
    slow_ema = compute_ema_from_values(closes, slow)
    macd = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd[i] = fast_ema[i] - slow_ema[i]
    sig = compute_ema_from_values(macd, signal)
    hist = [None] * n
    for i in range(n):
        if macd[i] is not None and sig[i] is not None:
            hist[i] = macd[i] - sig[i]
    return macd, sig, hist


def compute_ema_from_values(values, period=3):
    """Compute EMA of a list that may contain Nones."""
    n = len(values)
    result = [None] * n
    k = 2 / (period + 1)
    ema = None
    for i in range(n):
        v = values[i]
        if v is None:
            continue
        ema = v if ema is None else v * k + ema * (1 - k)
        result[i] = round(ema, 4)
    return result


def compute_ma(values, period=50):
    """Compute SMA from a list."""
    ma = [None] * len(values)
    for i in range(period - 1, len(values)):
        ma[i] = round(sum(values[i - period + 1: i + 1]) / period, 2)
    return ma


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        ticker = params.get("ticker", ["AAPL"])[0].upper()
        period = params.get("period", ["1y"])[0]
        start_ts = params.get("start", [None])[0]
        end_ts = params.get("end", [None])[0]

        # Configurable indicator parameters
        ma_period = int(params.get("ma_period", [200])[0])
        rsi_period = int(params.get("rsi_period", [14])[0])
        st_period = int(params.get("st_period", [20])[0])
        st_mult = float(params.get("st_mult", [3.0])[0])
        cmf_period = int(params.get("cmf_period", [20])[0])
        atr_period = int(params.get("atr_period", [14])[0])
        macd_fast = int(params.get("macd_fast", [12])[0])
        macd_slow = int(params.get("macd_slow", [26])[0])
        macd_signal = int(params.get("macd_signal", [9])[0])
        macdv_fast = int(params.get("macdv_fast", [12])[0])
        macdv_slow = int(params.get("macdv_slow", [26])[0])
        macdv_signal = int(params.get("macdv_signal", [9])[0])
        macdv_atr = int(params.get("macdv_atr", [26])[0])

        if period not in PERIOD_DAYS:
            period = "1y"

        # Fetch extra lookback data for indicator warm-up
        LOOKBACK_DAYS = int(ma_period * 1.5) + 30
        try:
            if start_ts is not None:
                lookback_start = int(start_ts) - LOOKBACK_DAYS * 86400
                all_rows, meta = fetch_yahoo_data(ticker, period,
                                                  str(lookback_start),
                                                  end_ts)
                # Find the trim point: first row at or after the requested start
                requested_start = datetime.utcfromtimestamp(int(start_ts)).strftime("%Y-%m-%d")
                trim_idx = 0
                for j, r in enumerate(all_rows):
                    if r["date"] >= requested_start:
                        trim_idx = j
                        break
            else:
                days = PERIOD_DAYS.get(period, 365)
                now = int(datetime.now().timestamp())
                lookback_start = int((datetime.now() - timedelta(days=days + LOOKBACK_DAYS)).timestamp())
                all_rows, meta = fetch_yahoo_data(ticker, period,
                                                  str(lookback_start),
                                                  str(now))
                # Trim to the originally requested window
                requested_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                trim_idx = 0
                for j, r in enumerate(all_rows):
                    if r["date"] >= requested_start:
                        trim_idx = j
                        break
        except Exception as e:
            self._send_json({"error": f"Failed to fetch data for {ticker}: {e}", "trace": traceback.format_exc()}, 500)
            return

        if not all_rows:
            self._send_json({"error": f"No data found for {ticker}"}, 404)
            return

        # Compute indicators on FULL dataset (including lookback)
        closes = [r["close"] for r in all_rows]
        highs = [r["high"] for r in all_rows]
        lows = [r["low"] for r in all_rows]
        volumes = [r["volume"] for r in all_rows]

        rsi = compute_rsi(closes, rsi_period)
        rsi_ema3 = compute_ema_from_values(rsi, 3)
        atr = compute_atr(highs, lows, closes, atr_period)
        macd_line, macd_sig, macd_hist = compute_macd(closes, macd_fast, macd_slow, macd_signal)
        macdv_line, macdv_sig, macdv_hist = compute_macd_v(closes, highs, lows, macdv_fast, macdv_slow, macdv_signal, macdv_atr)
        supertrend, st_dir = compute_supertrend(highs, lows, closes, st_period, st_mult)
        ma_vals = compute_ma(closes, ma_period)
        vol_ma14 = compute_ma(volumes, 14)
        cmf_vals = compute_cmf(highs, lows, closes, volumes, cmf_period)
        cmf_signal = compute_ema_from_values(cmf_vals, 3)

        # Build output arrays only for the requested window (trim_idx onwards)
        candles = []
        volume_data = []
        vol_ma_data = []
        rsi_data = []
        rsi_ema3_data = []
        atr_data = []
        macd_data = []
        macd_signal_data = []
        macd_hist_data = []
        macdv_data = []
        macdv_signal_data = []
        macdv_hist_data = []
        supertrend_data = []
        ma_data = []
        cmf_data = []
        cmf_signal_data = []

        for i in range(trim_idx, len(all_rows)):
            r = all_rows[i]
            t = r["date"]
            o, h, l, c, v = r["open"], r["high"], r["low"], r["close"], r["volume"]

            candles.append({"time": t, "open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2)})
            prev_close = all_rows[i - 1]["close"] if i > 0 else o
            volume_data.append({
                "time": t, "value": v,
                "color": "rgba(38,166,154,0.5)" if c >= prev_close else "rgba(239,83,80,0.5)",
            })

            if vol_ma14[i] is not None:
                vol_ma_data.append({"time": t, "value": vol_ma14[i]})

            if rsi[i] is not None:
                rsi_data.append({"time": t, "value": rsi[i]})

            if rsi_ema3[i] is not None:
                rsi_ema3_data.append({"time": t, "value": rsi_ema3[i]})

            if atr[i] is not None:
                atr_data.append({"time": t, "value": round(atr[i], 4)})

            if macd_line[i] is not None:
                macd_data.append({"time": t, "value": round(macd_line[i], 4)})
            if macd_sig[i] is not None:
                macd_signal_data.append({"time": t, "value": round(macd_sig[i], 4)})
            if macd_hist[i] is not None:
                h = round(macd_hist[i], 4)
                color = "rgba(38,166,154,0.6)" if h >= 0 else "rgba(239,83,80,0.6)"
                macd_hist_data.append({"time": t, "value": h, "color": color})

            if macdv_line[i] is not None:
                macdv_data.append({"time": t, "value": round(macdv_line[i], 2)})
            if macdv_sig[i] is not None:
                macdv_signal_data.append({"time": t, "value": round(macdv_sig[i], 2)})
            if macdv_hist[i] is not None:
                hv = round(macdv_hist[i], 2)
                color_v = "rgba(38,166,154,0.6)" if hv >= 0 else "rgba(239,83,80,0.6)"
                macdv_hist_data.append({"time": t, "value": hv, "color": color_v})

            if supertrend[i] is not None:
                st_val = round(supertrend[i], 2)
                st_color = "#26a69a" if st_dir[i] == 1 else "#ef5350"
                supertrend_data.append({"time": t, "value": st_val, "color": st_color})

            if ma_vals[i] is not None:
                ma_data.append({"time": t, "value": ma_vals[i]})

            if cmf_vals[i] is not None:
                cmf_data.append({"time": t, "value": cmf_vals[i]})

            if cmf_signal[i] is not None:
                cmf_signal_data.append({"time": t, "value": cmf_signal[i]})

        self._send_json({
            "ticker": ticker,
            "name": meta.get("longName") or meta.get("shortName", ""),
            "exchange": meta.get("exchangeName", ""),
            "candles": candles,
            "volume": volume_data,
            "vol_ma14": vol_ma_data,
            "rsi": rsi_data,
            "rsi_ema3": rsi_ema3_data,
            "atr": atr_data,
            "macd": macd_data,
            "macd_signal": macd_signal_data,
            "macd_hist": macd_hist_data,
            "macdv": macdv_data,
            "macdv_signal": macdv_signal_data,
            "macdv_hist": macdv_hist_data,
            "supertrend": supertrend_data,
            "ma": ma_data,
            "cmf": cmf_data,
            "cmf_signal": cmf_signal_data,
        })

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
