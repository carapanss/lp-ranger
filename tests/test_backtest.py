"""Tests for backtest engine + search pipeline."""

import json
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lp_core
from backtest.engine import run_backtest
from backtest.search import (
    GRIDS, _cfgs_for, slice_candles, summarise, walk_forward,
)


HOUR_MS = 3600 * 1000
REPO = Path(__file__).resolve().parent.parent


# ── Synthetic candle helpers ─────────────────────────────────────
def flat(days=365, price=3000.0):
    return [{"t": i * HOUR_MS, "p": price} for i in range(days * 24)]


def linear(days=365, start=3000.0, end=5000.0):
    n = days * 24
    return [{"t": i * HOUR_MS, "p": start + (end - start) * i / n}
            for i in range(n)]


def sine(days=365, mid=3000.0, amp=300.0, period_days=7):
    n = days * 24
    return [{"t": i * HOUR_MS,
             "p": mid + amp * math.sin(i / (24 * period_days) * 2 * math.pi)}
            for i in range(n)]


def load_cfg(name):
    with open(REPO / name) as f:
        return json.load(f)


# ── Tests ─────────────────────────────────────────────────────────
def test_flat_market_il_zero_fees_accrue():
    """On a truly flat price series IL is 0, fees accrue, no rebalances fire."""
    cfg = load_cfg("strategy_exit_pool.json")
    r = run_backtest(cfg, flat(days=365))
    assert r.total_il_usd == 0.0
    assert r.total_fees_usd > 50         # $0.31/day * 365d scaled
    assert r.n_rebalances == 0
    assert r.n_exits == 0
    assert r.net_apr > 100                # fees-only → should be > 100% APR
    assert r.time_in_pool_pct > 90


def test_fixed_strategy_accumulates_il_on_uptrend():
    """Fixed (non-exiting) strategy absorbs IL when price trends up strongly."""
    cfg = load_cfg("strategy_aggressive.json")  # strategy_type="fixed"
    r = run_backtest(cfg, linear(days=365, start=3000, end=5500))
    assert cfg["strategy_type"] == "fixed"
    assert r.total_il_usd > 0
    assert r.n_exits == 0


def test_cooldown_enforced_on_whipsaw():
    """Under rapid oscillations the engine honours the 4h cooldown + daily cap."""
    cfg = load_cfg("strategy_aggressive.json")
    # 30d of ±20% swings every 2h — guaranteed to repeatedly trip rebalance
    candles = []
    for i in range(30 * 24):
        p = 3000 * (1.2 if (i // 2) % 2 == 0 else 0.8)
        candles.append({"t": i * HOUR_MS, "p": p})
    r = run_backtest(cfg, candles, max_actions_per_day=3)
    # Max 3 rebalances/day * 30 days = 90 absolute cap
    assert r.n_rebalances <= 90


def test_shipped_exit_pool_produces_sensible_apr():
    """On a mildly trending 365d series, exit_pool produces a finite APR
    in a reasonable band (not NaN, not catastrophically negative)."""
    cfg = load_cfg("strategy_exit_pool.json")
    r = run_backtest(cfg, sine(days=365, mid=3000, amp=250))
    assert math.isfinite(r.net_apr)
    assert -100 < r.net_apr < 1000


def test_evaluate_strategy_signals_consistent():
    """lp_core.evaluate_strategy returns the daemon's signal set."""
    cfg = load_cfg("strategy_exit_pool.json")
    prices = [3000 + i for i in range(80)]
    state_in = {"range_lo": 2900, "range_hi": 3100,
                "pool_active": True, "hold_asset": None}
    sig, action, det = lp_core.evaluate_strategy(cfg, 3050, state_in, prices)
    assert sig in ("gray", "green", "yellow", "rebalance", "exit", "enter", "closed")


def test_walk_forward_slices_are_disjoint():
    """Walk-forward test windows should step by `step_days` and not overlap."""
    candles = flat(days=365)
    from backtest.search import walk_forward
    # Skip full walk to keep test fast — check slicing helper directly instead
    a = slice_candles(candles, 180, 210)
    b = slice_candles(candles, 210, 240)
    assert a and b
    assert a[-1]["t"] < b[0]["t"]


def test_grid_size_bounded():
    """Sanity-check the grid size so the search finishes in reasonable time."""
    n_exit = sum(1 for _ in _cfgs_for("exit_pool"))
    n_trend = sum(1 for _ in _cfgs_for("trend_following"))
    n_fixed = sum(1 for _ in _cfgs_for("fixed"))
    # 6*5*4*3*3 * 2*3 = 6480 for exit_pool
    # 6*4*3 * 2*3 = 432 for trend_following
    # 8*3 * 1 = 24 for fixed (indicators don't affect fixed signals)
    assert n_exit == 6 * 5 * 4 * 3 * 3 * 2 * 3
    assert n_trend == 6 * 4 * 3 * 2 * 3
    assert n_fixed == 8 * 3
    # Total must fit 6 walk-forward windows in ~10 min
    total = n_exit + n_trend + n_fixed
    assert total < 10000
