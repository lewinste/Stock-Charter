"""Regression tests for the Smoothness indicator.

Motivated by LDOS 2026-05-06..2026-07-30: price traced an orderly arc (steady
decline to 100, turn, steady recovery to 112) yet the plotted indicator swept
4.8 -> 100.0. Two defects:

  1. the efficiency-ratio term measured NET PROGRESS, so an orderly round trip
     scored as maximum chaos (2026-07-20: net -0.10 over 20 bars on a path of
     28.30 -> ER 0.4%);
  2. the value was a percentile rank against the name's OWN trailing history,
     which is uniform by construction and therefore fills 0..100 for every
     stock no matter how smooth it actually is.

Run:  python tests/test_smoothness.py
"""
import importlib.util
import os
import statistics as st
import sys

_API = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api", "chart.py")
_spec = importlib.util.spec_from_file_location("chartmod", _API)
chartmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chartmod)
compute_smoothness = chartmod.compute_smoothness


# ---------------------------------------------------------------- synthetic data
def _bars(closes, wick=0.05):
    """OHLC from a close path: open at prior close, tight wicks (decisive bars)."""
    o, h, l, c = [], [], [], []
    for i, close in enumerate(closes):
        op = closes[i - 1] if i else close
        o.append(op)
        c.append(close)
        h.append(max(op, close) + wick)
        l.append(min(op, close) - wick)
    return o, h, l, c


def orderly_arc(n=140, slope=0.3, base=100.0):
    """Steady decline, clean turn, steady recovery — the LDOS shape."""
    half = n // 2
    closes = [base - slope * k for k in range(half)]
    bottom = closes[-1]
    closes += [bottom + slope * (k + 1) for k in range(n - half)]
    return _bars(closes)


def zigzag(n=140, amp=1.0, base=100.0):
    """Alternating up/down days with wide wicks — genuinely choppy."""
    closes = [base + (amp if k % 2 == 0 else -amp) for k in range(n)]
    o, h, l, c = _bars(closes, wick=0.0)
    h = [max(o[i], c[i]) + 2.0 * amp for i in range(n)]      # long wicks both sides
    l = [min(o[i], c[i]) - 2.0 * amp for i in range(n)]
    return o, h, l, c


def _defined(series):
    return [v for v in series if v is not None]


# ---------------------------------------------------------------------- the tests
def test_orderly_roundtrip_reads_smooth():
    """THE LDOS CASE: an orderly arc must read smooth even with zero net progress."""
    o, h, l, c = orderly_arc()
    sm, _ = compute_smoothness(o, h, l, c)
    vals = _defined(sm)
    assert vals, "no smoothness values produced"
    # bars around the turn are where the old net-progress term collapsed
    turn = vals[len(vals) // 2 - 5: len(vals) // 2 + 5]
    assert min(turn) >= 60, f"orderly arc read as rough at the turn: min {min(turn):.1f}"


def monotone_ramp(n=140, slope=0.3, base=100.0):
    """Same texture as the arc, but with full net progress — no turn."""
    return _bars([base + slope * k for k in range(n)])


def test_net_progress_does_not_drive_the_reading():
    """Arc and ramp have IDENTICAL bar texture; only net progress differs.

    The old efficiency-ratio term made the arc score far lower purely because
    it ended where it started. Smoothness must measure texture, not trend.
    """
    arc = st.mean(_defined(compute_smoothness(*orderly_arc())[0]))
    ramp = st.mean(_defined(compute_smoothness(*monotone_ramp())[0]))
    assert abs(arc - ramp) <= 12, (f"net progress drives the reading: "
                                   f"arc {arc:.1f} vs ramp {ramp:.1f}")


def test_zigzag_reads_rough():
    o, h, l, c = zigzag()
    sm, _ = compute_smoothness(o, h, l, c)
    vals = _defined(sm)
    assert vals, "no smoothness values produced"
    assert max(vals) <= 40, f"zigzag read as smooth: max {max(vals):.1f}"


def test_scale_is_absolute_not_self_referential():
    """A smooth name and a choppy name must land in DIFFERENT parts of the range.

    A percentile-vs-own-history transform puts both near 50 — that is the bug.
    """
    sm_smooth = _defined(compute_smoothness(*orderly_arc())[0])
    sm_rough = _defined(compute_smoothness(*zigzag())[0])
    gap = st.mean(sm_smooth) - st.mean(sm_rough)
    assert gap >= 30, (f"scale is self-referential: smooth {st.mean(sm_smooth):.1f} "
                       f"vs rough {st.mean(sm_rough):.1f} (gap {gap:.1f})")


def test_stable_on_a_stable_regime():
    """Texture is unchanged along the arc, so the line must not sweep the range."""
    sm = _defined(compute_smoothness(*orderly_arc())[0])
    daily = [abs(sm[i] - sm[i - 1]) for i in range(1, len(sm))]
    assert st.mean(daily) <= 3.0, f"mean 1-day move {st.mean(daily):.2f} pts is too jumpy"
    assert max(sm) - min(sm) <= 45, f"range {max(sm) - min(sm):.1f} pts over one regime"


def test_no_warmup_cliff_and_short_series_safe():
    """Fixed scale means values appear right after `period` bars.

    The old percentile transform additionally needed 60 prior raw values before
    emitting anything, so a short window rendered blank.
    """
    o, h, l, c = orderly_arc(n=40)
    sm, fast = compute_smoothness(o, h, l, c, period=20, smooth_span=5)
    assert len(sm) == 40 and len(fast) == 40
    assert _defined(sm), "no values on a 40-bar series (warmup cliff)"
    first = next(i for i, v in enumerate(sm) if v is not None)
    assert first <= 21, f"first value at bar {first}, expected right after the window"
    # degenerate input must not raise
    flat_o, flat_h, flat_l, flat_c = _bars([100.0] * 30, wick=0.0)
    compute_smoothness(flat_o, flat_h, flat_l, flat_c)
    compute_smoothness([], [], [], [])


def test_output_bounded():
    for gen in (orderly_arc, zigzag):
        for v in _defined(compute_smoothness(*gen())[0]):
            assert 0.0 <= v <= 100.0, f"out of bounds: {v}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
