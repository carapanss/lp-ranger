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


def _sqrt_price(price_usdc_per_eth):
    raw = price_usdc_per_eth / 1e12
    return raw ** 0.5


def _v3_amounts_from_liquidity(liquidity, range_lo, range_hi, price):
    if liquidity <= 0 or range_lo <= 0 or range_hi <= 0 or price <= 0:
        return 0.0, 0.0
    sqrt_p = _sqrt_price(price)
    sqrt_a = _sqrt_price(min(range_lo, range_hi))
    sqrt_b = _sqrt_price(max(range_lo, range_hi))
    if sqrt_p <= sqrt_a:
        amount0 = liquidity * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        amount1 = 0.0
    elif sqrt_p >= sqrt_b:
        amount0 = 0.0
        amount1 = liquidity * (sqrt_b - sqrt_a)
    else:
        amount0 = liquidity * (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
        amount1 = liquidity * (sqrt_p - sqrt_a)
    weth = amount0 / 1e18
    usdc = amount1 / 1e6
    return weth, usdc


def _v3_position_value(liquidity, range_lo, range_hi, price):
    weth, usdc = _v3_amounts_from_liquidity(liquidity, range_lo, range_hi, price)
    return weth * price + usdc


def _open_position_from_capital(capital_usd, range_lo, range_hi, price):
    """Convert USD capital into a V3 position at `price` for the target range."""
    if capital_usd <= 0 or range_lo <= 0 or range_hi <= 0 or price <= 0:
        return 0.0, 0.0, 0.0
    unit_weth, unit_usdc = _v3_amounts_from_liquidity(1.0, range_lo, range_hi, price)
    unit_value = unit_weth * price + unit_usdc
    if unit_value <= 0:
        return 0.0, 0.0, 0.0
    liquidity = capital_usd / unit_value
    weth, usdc = _v3_amounts_from_liquidity(liquidity, range_lo, range_hi, price)
    return liquidity, weth, usdc


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

    range_lo = 0.0
    range_hi = 0.0
    pool_basis = 0.0
    pool_liquidity = 0.0
    entry_weth = 0.0
    entry_usdc = 0.0
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
    lend_per_tick_factor = hours_per_candle / HOURS_PER_YEAR

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
        at = atr_arr[idx] if warmed else None
        vp = ((at / price) * 100.0) if (at is not None and price > 0) else 0.0

        # Bootstrap a pool on the first warmed tick
        if (warmed and not pool_active and hold_asset == "USDC"
                and eth_held == 0 and cash_usd > 0 and pool_liquidity == 0):
            width_pct = lp_core.target_width_pct(p, trend_pct=0.0, rsi_value=rs, vol_pct=vp)
            _, range_lo, range_hi = lp_core.target_center_and_range(
                price, width_pct, float(p.get("trend_shift", 0.4)), False, 0.0
            )
            pool_basis = cash_usd
            pool_liquidity, entry_weth, entry_usdc = _open_position_from_capital(
                pool_basis, range_lo, range_hi, price
            )
            cash_usd = 0
            pool_active = True
            last_action_s = t_s
            total_gas += gas_usd_per_action
            n_enters += 1

        # Mark-to-market
        if pool_active and pool_liquidity > 0 and price > 0:
            pool_mtm = _v3_position_value(pool_liquidity, range_lo, range_hi, price)
            hold_mtm = entry_weth * price + entry_usdc
        else:
            pool_mtm = 0.0
            hold_mtm = 0.0

        if not pool_active:
            cash_usd *= 1 + (float(p.get("idle_lend_usdc_apr", 0.0)) / 100.0) * lend_per_tick_factor
            eth_held *= 1 + (float(p.get("idle_lend_eth_apr", 0.0)) / 100.0) * lend_per_tick_factor

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
            # Scale by actual deployed pool capital, not total equity (which includes
            # accumulated fee cash that is not reinvested into the position).
            scale = (pool_mtm / position_usd) if pool_mtm > 0 else 1.0
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

        sig, action, _ = lp_core.evaluate_strategy_snapshot(
            cfg, price,
            {"range_lo": range_lo, "range_hi": range_hi,
             "pool_active": pool_active, "hold_asset": hold_asset},
            trend_up=tu, trend_pct=tp, rsi_value=rs, vol_pct=vp,
        )
        if action is None:
            continue
        if t_s - last_action_s < cooldown_s:
            continue
        if actions_today >= max_actions_per_day:
            continue

        atype = action["type"]
        total_gas += gas_usd_per_action
        last_action_s = t_s
        actions_today += 1

        if atype == "rebalance":
            il_usd = max(hold_mtm - pool_mtm, 0.0)
            total_il += il_usd
            pool_basis = pool_mtm
            if pool_basis < 0:
                pool_basis = 0
            range_lo = float(action["lo"])
            range_hi = float(action["hi"])
            pool_liquidity, entry_weth, entry_usdc = _open_position_from_capital(
                pool_basis, range_lo, range_hi, price
            )
            n_rebalances += 1

        elif atype == "exit_pool":
            il_usd = max(hold_mtm - pool_mtm, 0.0)
            total_il += il_usd
            mtm = pool_mtm
            if mtm < 0:
                mtm = 0
            if action["hold"] == "ETH":
                eth_held += mtm / price if price > 0 else 0
                hold_asset = "ETH"
            else:
                cash_usd += mtm
                hold_asset = "USDC"
            pool_basis = 0
            pool_liquidity = 0
            entry_weth = 0
            entry_usdc = 0
            range_lo = 0
            range_hi = 0
            pool_active = False
            n_exits += 1

        elif atype == "enter_pool":
            if hold_asset == "ETH":
                cash_usd += eth_held * price
                eth_held = 0
            pool_basis = cash_usd
            cash_usd = 0
            range_lo = float(action["lo"])
            range_hi = float(action["hi"])
            pool_liquidity, entry_weth, entry_usdc = _open_position_from_capital(
                pool_basis, range_lo, range_hi, price
            )
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
