"""Calibrate the fixed reference scale for the Smoothness indicator.

Measures the raw texture blend across a broad universe of daily bars and emits
the 5th-percentile cut points that api/chart.py maps through, so that a plotted
value of 50 means "as smooth as a typical stock-day" rather than "median for
this ticker".

Data source is the pinned raw (unadjusted) OHLCV cache in the trading-agent
project. Re-run only when the definition of the raw blend changes.

    python tools/calibrate_smoothness.py [--universe SP500-PIT-10y] [--period 20]
"""
import argparse
import importlib.util
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.environ.get(
    "TRADING_AGENT_DIR",
    "/Users/lewinste/Projects/Investing-Helper/trading-agent/agent")

_spec = importlib.util.spec_from_file_location(
    "chartmod", os.path.join(os.path.dirname(_HERE), "api", "chart.py"))
chartmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chartmod)


def raw_texture_np(o, h, l, c, period):
    """Vectorised twin of the raw blend inside chart.compute_smoothness.

    Verified against the pure-python original by _assert_agrees below.
    """
    n = len(c)
    d = np.diff(c, prepend=np.nan)                       # d[k] = c[k] - c[k-1]
    absd = np.abs(d)
    jerk_step = np.abs(np.diff(d, prepend=np.nan))       # |d[k] - d[k-1]|

    def roll_sum(a, w):
        cs = np.nancumsum(np.nan_to_num(a, nan=0.0))
        out = np.full(len(a), np.nan)
        out[w - 1:] = cs[w - 1:] - np.concatenate(([0.0], cs[:-w]))
        return out

    path = roll_sum(absd, period)                        # window = last `period` deltas
    # jerk pairs adjacent deltas WITHIN the window -> period-1 terms, so the
    # window is period-1 wide (a `period`-wide sum would reach one delta back
    # past the window edge).
    jerk = roll_sum(jerk_step, period - 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        curvature = np.clip(1.0 - jerk / (2.0 * path), 0.0, None)

        rng = h - l
        bodyf = np.where(rng > 0, np.abs(c - o) / np.where(rng > 0, rng, 1), np.nan)
        body_sum = roll_sum(np.where(np.isnan(bodyf), 0.0, bodyf), period)
        body_cnt = roll_sum((~np.isnan(bodyf)).astype(float), period)
        body = body_sum / body_cnt

    sign = np.sign(d)
    raw = np.full(n, np.nan)
    for i in range(period, n):
        s = sign[i - period + 1: i + 1]
        s = s[s != 0]
        if len(s) < 2:
            continue
        flips = int(np.sum(s[1:] != s[:-1]))
        non_flip = 1 - flips / (len(s) - 1)
        if not np.isfinite(curvature[i]) or not np.isfinite(body[i]) or path[i] == 0:
            continue
        raw[i] = (curvature[i] + body[i] + non_flip) / 3
    return raw


def _assert_agrees(frames, period):
    """The numpy twin must reproduce the shipped pure-python values exactly."""
    tick, df = next(iter(frames.items()))
    o, h, l, c = (df[x].astype(float).to_numpy() for x in ("Open", "High", "Low", "Close"))
    mine = raw_texture_np(o, h, l, c, period)
    _, fast = chartmod.compute_smoothness(list(o), list(h), list(l), list(c), period, 1)
    theirs = np.array([np.nan if v is None else v for v in fast])
    ref = chartmod._sm_reference(period)
    mapped = np.array([np.nan if not np.isfinite(v) else chartmod._sm_to_scale(v, ref)
                       for v in mine])
    ok = np.isfinite(mapped) & np.isfinite(theirs)
    assert ok.sum() > 100, f"too few comparable bars ({ok.sum()}) on {tick}"
    worst = np.max(np.abs(mapped[ok] - theirs[ok]))
    assert worst < 1e-6, f"numpy twin disagrees with chart.py on {tick}: max diff {worst}"
    print(f"  agreement check on {tick}: {ok.sum()} bars, max diff {worst:.2e}  OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="SP500-PIT-10y")
    ap.add_argument("--period", type=int, default=20)
    args = ap.parse_args()

    import pandas as pd
    pkl = os.path.join(_AGENT, ".ohlcv_cache", f"{args.universe}.pkl")
    frames = pd.read_pickle(pkl)
    frames = {t: df for t, df in frames.items() if df is not None and len(df) > args.period + 50}
    print(f"universe {args.universe}: {len(frames)} tickers")

    _assert_agrees(frames, args.period)

    pool = []
    for t, df in frames.items():
        o, h, l, c = (df[x].astype(float).to_numpy() for x in ("Open", "High", "Low", "Close"))
        r = raw_texture_np(o, h, l, c, args.period)
        pool.append(r[np.isfinite(r)])
    pool = np.concatenate(pool)
    print(f"pooled bars: {len(pool):,}")

    qs = np.arange(0, 101, 5)
    cuts = np.percentile(pool, qs)
    print("\npercentile -> raw cut point")
    for q, v in zip(qs, cuts):
        print(f"  {q:3d}  {v:.4f}")
    print(f"\nmean {pool.mean():.4f}  std {pool.std():.4f}")

    body = ",\n                ".join(
        ", ".join(f"{v:.4f}" for v in cuts[i:i + 5]) for i in range(0, len(cuts), 5))
    print("\n--- paste into api/chart.py ---")
    print(f"SM_REFERENCE = [{body}]")


if __name__ == "__main__":
    main()
