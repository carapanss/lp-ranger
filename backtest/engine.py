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
                 cooldown_hours=DEFAULT_COOLDOWN_H,
                 max_actions_per_day=DEFAULT_MAX_ACTIONS_PER_DAY,
                 hist_cap=400) -> BacktestResult:
    """Replay the candle stream under `cfg`. Returns a BacktestResult.

    Candle shape: {'t': unix_ms, 'p': close_usd}.
    Assumes hourly candles (scales fee accrual by 1/24 of the daily rate).
    """
    if not candles:
        return BacktestResult()

    errs = lp_core.validate_strategy(cfg)
    if errs:
        raise ValueError(f"invalid cfg: {errs}")

    # ── Determine initial state: open pool at first warm-up tick ──
    ic = cfg.get("data_sources", {}).get("indicators", {})
    need = lp_core.warm_samples(ic)

    # Local mutable state (avoid reconstructing dicts each tick)
    range_lo = 0.0
    range_hi = 0.0
    entry_price = 0.0          # price at which current range was opened (midpoint)
    pool_basis = 0.0           # USD basis tracked inside the pool
    cash_usd = position_usd    # start in USDC
    eth_held = 0.0
    pool_active = False
    hold_asset = "USDC"
    price_hist: list[float] = []

    last_action_s = -10 * 86400
    actions_today = 0
    current_day_idx = -1
    cooldown_s = cooldown_hours * 3600

    # Hourly candle spacing check (for fee scaling)
    if len(candles) >= 2:
        hours_per_candle = (candles[1]["t"] - candles[0]["t"]) / 3600000.0
    else:
        hours_per_candle = 1.0
    if hours_per_candle <= 0:
        hours_per_candle = 1.0

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

    start_ts = candles[0]["t"]
    end_ts = candles[-1]["t"]

    for candle in candles:
        t_ms = candle["t"]
        price = float(candle["p"])
        t_s = t_ms / 1000.0

        # Day-boundary reset
        day_idx = int(t_s // 86400)
        if day_idx != current_day_idx:
            current_day_idx = day_idx
            actions_today = 0

        price_hist.append(price)
        if len(price_hist) > hist_cap:
            price_hist = price_hist[-hist_cap:]

        # ── Bootstrap: once warmed up, open a pool at first green-ish tick ──
        if not pool_active and hold_asset == "USDC" and eth_held == 0 and \
                cash_usd > 0 and len(price_hist) >= need and entry_price == 0:
            bw = float(cfg.get("parameters", {}).get("base_width_pct",
                       cfg.get("parameters", {}).get("width_pct", 15)))
            range_lo = price * (1 - bw / 200)
            range_hi = price * (1 + bw / 200)
            entry_price = price
            pool_basis = cash_usd
            cash_usd = 0
            pool_active = True
            last_action_s = t_s
            total_gas += gas_usd_per_action
            n_enters += 1

        # Mark-to-market equity — always sum all three buckets
        pool_mtm = (_pool_value(pool_basis, range_lo, range_hi, entry_price, price)
                    if pool_active else 0.0)
        equity = pool_mtm + cash_usd + eth_held * price

        equity_curve.append((t_ms, equity))

        if pool_active:
            time_in_pool += 1
        elif hold_asset == "ETH":
            time_in_eth += 1
        else:
            time_in_usdc += 1

        # ── Fees (only when in-range & pool active) ──
        if pool_active and range_lo <= price <= range_hi:
            width_pct = (range_hi - range_lo) / ((range_lo + range_hi) / 2) * 100
            daily_fee = fee_daily_base * (fee_width_ref / max(width_pct, 1.0))
            # Scale the daily rate to per candle and proportionally to the
            # user's actual position_usd vs the $119.50 reference the
            # constants were fitted on.
            scale = (equity / 119.50) if equity > 0 else 1.0
            fee_this_tick = daily_fee * (hours_per_candle / 24.0) * scale
            total_fees += fee_this_tick
            cash_usd += fee_this_tick  # bank fees as cash so they don't re-enter IL

        # ── Strategy signal ──
        state = {"range_lo": range_lo, "range_hi": range_hi,
                 "pool_active": pool_active, "hold_asset": hold_asset}
        signal, action, _ = lp_core.evaluate_strategy(cfg, price, state, price_hist)

        if not action:
            continue

        # ── Cooldown + daily cap ──
        if t_s - last_action_s < cooldown_s:
            continue
        if actions_today >= max_actions_per_day:
            continue

        atype = action["type"]
        total_gas += gas_usd_per_action
        last_action_s = t_s
        actions_today += 1

        if atype == "rebalance":
            # Realize IL on the outgoing range
            _, il_usd = lp_core.il_estimate(range_lo, range_hi, price, pool_basis)
            total_il += il_usd
            # Update position basis to current MTM (keeping fees captured on cash side)
            pool_basis = _pool_value(pool_basis, range_lo, range_hi, entry_price, price) - il_usd
            if pool_basis < 0:
                pool_basis = 0
            range_lo = float(action["lo"])
            range_hi = float(action["hi"])
            entry_price = price
            n_rebalances += 1

        elif atype == "exit_pool":
            # Realize IL, then convert the pool basis into ETH or USDC
            _, il_usd = lp_core.il_estimate(range_lo, range_hi, price, pool_basis)
            total_il += il_usd
            mtm = _pool_value(pool_basis, range_lo, range_hi, entry_price, price) - il_usd
            if mtm < 0:
                mtm = 0
            held = action.get("hold", "USDC")
            if held == "ETH":
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
            # Move cash + eth into a fresh pool at the given range
            if hold_asset == "ETH":
                cash_usd += eth_held * price
                eth_held = 0
            pool_basis = cash_usd
            cash_usd = 0
            range_lo = float(action["lo"])
            range_hi = float(action["hi"])
            entry_price = price
            pool_active = True
            hold_asset = "USDC"  # reset on re-entry
            n_enters += 1

    # Final mark-to-market
    if equity_curve:
        final_equity = equity_curve[-1][1]
    else:
        final_equity = position_usd

    elapsed_ms = (end_ts - start_ts) if end_ts > start_ts else 1
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

    total_ticks = len(equity_curve)
    if total_ticks == 0:
        total_ticks = 1

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
