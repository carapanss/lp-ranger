"""Pure Uniswap V3 WETH/USDC LP backtest engine.

Takes a strategy cfg + an hourly candle stream and produces a PnL time
series plus summary metrics. No I/O, no globals — fully deterministic
given the inputs.

Reuses lp_core.{evaluate_strategy, il_estimate} for signal generation
and impermanent-loss accounting so the backtest and the live daemon
share one truth.
"""

from dataclasses import dataclass, field
from typing import Any

import lp_core


# ── Defaults tracking the live daemon ────────────────────────────
DEFAULT_POSITION_USD = 119.50
DEFAULT_GAS_USD = 1.00
DEFAULT_FEE_DAILY_BASE = 0.31          # $/day at reference width
DEFAULT_FEE_WIDTH_REF = 15.63          # reference range width (%)
DEFAULT_COOLDOWN_H = 4
DEFAULT_MAX_ACTIONS_PER_DAY = 3
HOURS_PER_YEAR = 24 * 365


@dataclass
class BacktestResult:
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    net_apr: float = 0.0
    total_fees_usd: float = 0.0
    total_il_usd: float = 0.0
    total_gas_usd: float = 0.0
    n_rebalances: int = 0
    n_exits: int = 0
    n_enters: int = 0
    max_drawdown_pct: float = 0.0
    time_in_pool_pct: float = 0.0
    time_in_eth_pct: float = 0.0
    time_in_usdc_pct: float = 0.0
    start_ts: int = 0
    end_ts: int = 0
    elapsed_days: float = 0.0


def _pool_value(position_usd_basis, range_lo, range_hi, entry_price, price):
    """Mark-to-market of a concentrated LP position. Uses the V2 IL formula
    times the V3 amplifier (via lp_core.il_estimate), which already caps
    IL at 20% per segment to avoid runaway estimates.

    Returns the current USD value of the position.
    """
    if position_usd_basis <= 0 or range_lo <= 0 or range_hi <= 0 or price <= 0:
        return position_usd_basis
    # IL relative to the snapshot taken at entry_price (middle of range by default)
    il_pct, _ = lp_core.il_estimate(range_lo, range_hi, price, position_usd_basis)
    # The position tracks price: roughly (price/entry)^0.5 for the non-IL factor.
    # For ranking consistency we approximate as: basis * (1 + (price/entry - 1) * 0.5) * (1 - il_pct)
    # (Gives a smooth equity curve without requiring the full LP liquidity math.)
    if entry_price <= 0:
        return position_usd_basis
    r = price / entry_price
    # Underlying V2 50/50: value_factor = 2*sqrt(r)/(1+r)
    import math
    factor = 2 * math.sqrt(r) / (1 + r) if r > 0 else 1.0
    return position_usd_basis * factor  # il_pct is already embedded in the V2 factor


def run_backtest(cfg, candles, *,
                 position_usd=DEFAULT_POSITION_USD,
                 gas_usd_per_action=DEFAULT_GAS_USD,
                 fee_daily_base=DEFAULT_FEE_DAILY_BASE,
                 fee_width_ref=DEFAULT_FEE_WIDTH_REF,
                 cooldown_hours=None,
                 max_actions_per_day=None,
                 hist_cap=400,
                 indicators=None) -> BacktestResult:
    """Replay the candle stream under `cfg`. Returns a BacktestResult.

    Candle shape: {'t': unix_ms, 'p': close_usd}.
    Assumes hourly candles (scales fee accrual by 1/24 of the daily rate).

    If `indicators` is provided (a dict with keys ema_fast/ema_slow/atr/rsi,
    each a list[float|None] aligned with candles), the hot inner loop uses
    them directly — 50-100x faster than recomputing per tick. Otherwise the
    series are computed once from `candles` at entry.
    """
    if not candles:
        return BacktestResult()

    errs = lp_core.validate_strategy(cfg)
    if errs:
        raise ValueError(f"invalid cfg: {errs}")

    ic = cfg.get("data_sources", {}).get("indicators", {})
    need = lp_core.warm_samples(ic)

    # Precompute indicator streams if not supplied
    if indicators is None:
        prices_arr = [c["p"] for c in candles]
        indicators = {
            "ema_fast": lp_core.ema_series(prices_arr, int(ic.get("ema_fast", 20))),
            "ema_slow": lp_core.ema_series(prices_arr, int(ic.get("ema_slow", 50))),
            "atr":      lp_core.atr_series(prices_arr, int(ic.get("atr_period", 14))),
            "rsi":      lp_core.rsi_series(prices_arr, int(ic.get("rsi_period", 14))),
        }
    ema_fast_arr = indicators["ema_fast"]
    ema_slow_arr = indicators["ema_slow"]
    atr_arr = indicators["atr"]
    rsi_arr = indicators["rsi"]

    p = cfg.get("parameters", {})
    strategy_type = cfg.get("strategy_type", "exit_pool")
    execution = cfg.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    base_width_pct = float(p.get("base_width_pct", p.get("width_pct", 15)))
    trend_shift = float(p.get("trend_shift", 0.4))
    buffer_pct = float(p.get("buffer_pct", 5))
    exit_trend_pct = float(p.get("exit_trend_pct", 10))
    enter_trend_pct = float(p.get("enter_trend_pct", 2))

    range_lo = 0.0
    range_hi = 0.0
    entry_price = 0.0
    pool_basis = 0.0
    cash_usd = position_usd
    eth_held = 0.0
    pool_active = False
    hold_asset = "USDC"

    if cooldown_hours is None:
        cooldown_hours = float(execution.get("cooldown_seconds", DEFAULT_COOLDOWN_H * 3600)) / 3600.0
    if max_actions_per_day is None:
        max_actions_per_day = int(execution.get("max_actions_per_day", DEFAULT_MAX_ACTIONS_PER_DAY))

    last_action_s = -10 * 86400
    actions_today = 0
    current_day_idx = -1
    cooldown_s = cooldown_hours * 3600

    if len(candles) >= 2:
        hours_per_candle = (candles[1]["t"] - candles[0]["t"]) / 3600000.0
    else:
        hours_per_candle = 1.0
    if hours_per_candle <= 0:
        hours_per_candle = 1.0
    fee_per_tick_factor = hours_per_candle / 24.0

    equity_curve: list[tuple[int, float]] = []
    total_fees = 0.0
    total_il = 0.0
    total_gas = 0.0
    n_rebalances = 0
    n_exits = 0
    n_enters = 0
    time_in_pool = 0
    time_in_eth = 0
    time_in_usdc = 0

    import math as _m

    for idx, candle in enumerate(candles):
        t_ms = candle["t"]
        price = float(candle["p"])
        t_s = t_ms / 1000.0

        day_idx = int(t_s // 86400)
        if day_idx != current_day_idx:
            current_day_idx = day_idx
            actions_today = 0

        # Warm-up gate
        warmed = idx >= need
        ef = ema_fast_arr[idx] if warmed else None
        es = ema_slow_arr[idx] if warmed else None
        rs = rsi_arr[idx] if warmed else None

        # Bootstrap a pool on the first warmed tick
        if (warmed and not pool_active and hold_asset == "USDC"
                and eth_held == 0 and cash_usd > 0 and entry_price == 0):
            range_lo = price * (1 - base_width_pct / 200)
            range_hi = price * (1 + base_width_pct / 200)
            entry_price = price
            pool_basis = cash_usd
            cash_usd = 0
            pool_active = True
            last_action_s = t_s
            total_gas += gas_usd_per_action
            n_enters += 1

        # Mark-to-market
        if pool_active and entry_price > 0 and price > 0:
            r_p = price / entry_price
            factor = 2 * _m.sqrt(r_p) / (1 + r_p) if r_p > 0 else 1.0
            pool_mtm = pool_basis * factor
        else:
            pool_mtm = 0.0
        equity = pool_mtm + cash_usd + eth_held * price
        equity_curve.append((t_ms, equity))

        if pool_active:
            time_in_pool += 1
        elif hold_asset == "ETH":
            time_in_eth += 1
        else:
            time_in_usdc += 1

        # Fees while in-range
        if pool_active and range_lo <= price <= range_hi:
            width_pct = (range_hi - range_lo) / ((range_lo + range_hi) / 2) * 100
            daily_fee = fee_daily_base * (fee_width_ref / max(width_pct, 1.0))
            scale = (equity / 119.50) if equity > 0 else 1.0
            fee_this_tick = daily_fee * fee_per_tick_factor * scale
            total_fees += fee_this_tick
            cash_usd += fee_this_tick

        if not warmed:
            continue

        # Compute trend signals inline
        if ef is None or es is None or es == 0:
            continue
        tp = (ef - es) / es * 100
        tu = ef > es

        action = None

        if not pool_active:
            if abs(tp) < enter_trend_pct:
                hw = base_width_pct / 200
                sh = hw * trend_shift * min(abs(tp) / 100 * 8, 1)
                nc = price * (1 + sh) if tu else price * (1 - sh)
                action = ("enter_pool",
                          nc * (1 - base_width_pct / 200),
                          nc * (1 + base_width_pct / 200),
                          None)
            elif hold_asset == "ETH" and rs is not None and rs < 35:
                action = ("enter_pool",
                          price * (1 - base_width_pct / 200),
                          price * (1 + base_width_pct / 200),
                          None)
            elif hold_asset == "USDC" and rs is not None and rs > 65:
                action = ("enter_pool",
                          price * (1 - base_width_pct / 200),
                          price * (1 + base_width_pct / 200),
                          None)
        else:
            inr = range_lo <= price <= range_hi
            if strategy_type == "exit_pool" and abs(tp) > exit_trend_pct:
                action = ("exit_pool", 0, 0, "ETH" if tp > 0 else "USDC")
            elif (not inr and
                  (price < range_lo * (1 - buffer_pct / 100)
                   or price > range_hi * (1 + buffer_pct / 100))):
                hw = base_width_pct / 200
                sh = hw * trend_shift * min(abs(tp) / 100 * 8, 1)
                nc = price * (1 + sh) if tu else price * (1 - sh)
                action = ("rebalance",
                          nc * (1 - base_width_pct / 200),
                          nc * (1 + base_width_pct / 200),
                          None)

        if action is None:
            continue
        if t_s - last_action_s < cooldown_s:
            continue
        if actions_today >= max_actions_per_day:
            continue

        atype, alo, ahi, ahold = action
        total_gas += gas_usd_per_action
        last_action_s = t_s
        actions_today += 1

        if atype == "rebalance":
            _, il_usd = lp_core.il_estimate(range_lo, range_hi, price, pool_basis)
            total_il += il_usd
            pool_basis = pool_mtm - il_usd
            if pool_basis < 0:
                pool_basis = 0
            range_lo = alo
            range_hi = ahi
            entry_price = price
            n_rebalances += 1

        elif atype == "exit_pool":
            _, il_usd = lp_core.il_estimate(range_lo, range_hi, price, pool_basis)
            total_il += il_usd
            mtm = pool_mtm - il_usd
            if mtm < 0:
                mtm = 0
            if ahold == "ETH":
                eth_held += mtm / price if price > 0 else 0
                hold_asset = "ETH"
            else:
                cash_usd += mtm
                hold_asset = "USDC"
            pool_basis = 0
            range_lo = 0
            range_hi = 0
            entry_price = 0
            pool_active = False
            n_exits += 1

        elif atype == "enter_pool":
            if hold_asset == "ETH":
                cash_usd += eth_held * price
                eth_held = 0
            pool_basis = cash_usd
            cash_usd = 0
            range_lo = alo
            range_hi = ahi
            entry_price = price
            pool_active = True
            hold_asset = "USDC"
            n_enters += 1

    # Summary
    final_equity = equity_curve[-1][1] if equity_curve else position_usd
    start_ts = candles[0]["t"]
    end_ts = candles[-1]["t"]
    elapsed_ms = max(end_ts - start_ts, 1)
    elapsed_days = elapsed_ms / 86400000.0
    if elapsed_days <= 0:
        elapsed_days = 1 / 24
    net_apr = ((final_equity / position_usd) - 1) * (365.0 / elapsed_days) * 100

    peak = position_usd
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    total_ticks = len(equity_curve) or 1

    return BacktestResult(
        equity_curve=equity_curve,
        net_apr=round(net_apr, 2),
        total_fees_usd=round(total_fees, 2),
        total_il_usd=round(total_il, 2),
        total_gas_usd=round(total_gas, 2),
        n_rebalances=n_rebalances,
        n_exits=n_exits,
        n_enters=n_enters,
        max_drawdown_pct=round(max_dd, 2),
        time_in_pool_pct=round(time_in_pool / total_ticks * 100, 1),
        time_in_eth_pct=round(time_in_eth / total_ticks * 100, 1),
        time_in_usdc_pct=round(time_in_usdc / total_ticks * 100, 1),
        start_ts=start_ts,
        end_ts=end_ts,
        elapsed_days=round(elapsed_days, 1),
    )


def precompute_indicators(candles, ic):
    """Return {ema_fast, ema_slow, atr, rsi} streams aligned with `candles`.

    Shared across all configs with the same indicator combo to amortise
    the O(n) work of a full indicator sweep.
    """
    prices = [c["p"] for c in candles]
    return {
        "ema_fast": lp_core.ema_series(prices, int(ic.get("ema_fast", 20))),
        "ema_slow": lp_core.ema_series(prices, int(ic.get("ema_slow", 50))),
        "atr":      lp_core.atr_series(prices, int(ic.get("atr_period", 14))),
        "rsi":      lp_core.rsi_series(prices, int(ic.get("rsi_period", 14))),
    }
