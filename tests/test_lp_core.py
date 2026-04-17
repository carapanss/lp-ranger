"""Pytest suite for lp_core (pure helpers).

Run with:  pytest -q  (from the repo root)
"""

import json
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import lp_core  # noqa: E402


# ── validate_strategy ──────────────────────────────────────────

def _shipped_strategy(name):
    return json.loads((REPO / name).read_text())


def test_shipped_strategies_are_valid():
    for name in ("strategy_exit_pool.json",
                 "strategy_v1.json",
                 "strategy_aggressive.json"):
        cfg = _shipped_strategy(name)
        assert lp_core.validate_strategy(cfg) == [], f"{name} must validate"


def test_rejects_unknown_strategy_type():
    errs = lp_core.validate_strategy({"strategy_type": "bogus", "parameters": {}})
    assert any("strategy_type" in e for e in errs)


def test_rejects_out_of_range_width():
    errs = lp_core.validate_strategy({"strategy_type": "exit_pool",
                                      "parameters": {"base_width_pct": 200}})
    assert any("base_width_pct" in e for e in errs)


def test_rejects_non_numeric_param():
    errs = lp_core.validate_strategy({"strategy_type": "exit_pool",
                                      "parameters": {"buffer_pct": "abc"}})
    assert any("buffer_pct" in e for e in errs)


def test_rejects_non_positive_indicator_period():
    errs = lp_core.validate_strategy({
        "strategy_type": "exit_pool",
        "parameters": {},
        "data_sources": {"indicators": {"ema_fast": 0}},
    })
    assert any("ema_fast" in e for e in errs)


def test_rejects_non_dict():
    assert lp_core.validate_strategy("not a dict")
    assert lp_core.validate_strategy(None)


# ── warm_samples ───────────────────────────────────────────────

def test_warm_samples_picks_largest_requirement():
    assert lp_core.warm_samples({"ema_slow": 50, "rsi_period": 14, "atr_period": 14}) == 50
    # rsi_period+1 dominates if rsi is bigger
    assert lp_core.warm_samples({"ema_slow": 10, "rsi_period": 20, "atr_period": 5}) == 21


# ── ema / atr / rsi ────────────────────────────────────────────

def test_ema_returns_none_when_insufficient():
    assert lp_core.ema([1, 2, 3], 5) is None


def test_ema_matches_sma_for_flat_series():
    # A flat series of N equal prices collapses to that price.
    assert lp_core.ema([100.0] * 30, 20) == 100.0


def test_ema_tracks_upward_trend():
    prices = list(range(1, 101))  # 1..100 linear
    fast = lp_core.ema(prices, 20)
    slow = lp_core.ema(prices, 50)
    # On a rising linear series, fast EMA must lead slow EMA.
    assert fast is not None and slow is not None
    assert fast > slow


def test_atr_returns_none_when_insufficient():
    assert lp_core.atr([100, 101], 14) is None


def test_atr_flat_series_is_zero():
    assert lp_core.atr([100.0] * 30, 14) == 0.0


def test_atr_grows_with_volatility():
    calm = [100.0 + (i % 2) * 0.1 for i in range(30)]  # ±0.1
    wild = [100.0 + (i % 2) * 5.0 for i in range(30)]  # ±5
    assert lp_core.atr(wild, 14) > lp_core.atr(calm, 14)


def test_rsi_returns_none_when_insufficient():
    assert lp_core.rsi([1, 2, 3], 14) is None


def test_rsi_saturates_on_pure_uptrend():
    prices = [100 + i for i in range(30)]
    assert lp_core.rsi(prices, 14) == 100


def test_rsi_saturates_on_pure_downtrend():
    prices = [100 - i for i in range(30)]
    assert lp_core.rsi(prices, 14) == 0


def test_rsi_flat_series_is_neutral():
    assert lp_core.rsi([100.0] * 30, 14) == 50


# ── il_estimate ────────────────────────────────────────────────

def test_il_same_price_is_zero():
    il_pct, il_usd = lp_core.il_estimate(2100, 2300, 2200, 119.50)
    assert il_pct == 0.0
    assert il_usd == 0.0


def test_il_scales_with_position_value():
    _, il_small = lp_core.il_estimate(2100, 2300, 2500, 100)
    _, il_big = lp_core.il_estimate(2100, 2300, 2500, 1000)
    assert math.isclose(il_big, il_small * 10, rel_tol=1e-9)


def test_il_narrower_range_amplifies():
    # Same price move, narrower range → bigger IL (up to 4x amp cap).
    _, il_wide = lp_core.il_estimate(1800, 2600, 2500, 119.50)
    _, il_narrow = lp_core.il_estimate(2150, 2250, 2500, 119.50)
    assert il_narrow > il_wide


def test_il_capped_at_20_percent():
    # Extreme divergence shouldn't report >20% IL.
    il_pct, _ = lp_core.il_estimate(2100, 2300, 10000, 119.50)
    assert il_pct <= 0.20


def test_il_bad_inputs_return_zero():
    assert lp_core.il_estimate(0, 0, 2200, 100) == (0.0, 0.0)
    assert lp_core.il_estimate(2100, 2300, 0, 100) == (0.0, 0.0)
    assert lp_core.il_estimate(2100, 2300, 2200, 0) == (0.0, 0.0)
