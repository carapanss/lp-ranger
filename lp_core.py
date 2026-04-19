"""Pure helpers shared by lp_ranger, lp_daemon and the test suite.

Everything in this module must be side-effect-free (no file I/O, no network,
no GTK, no globals mutated) so it can be unit-tested in isolation.
"""

import math


def validate_strategy(cfg):
    """Validate a strategy config dict. Returns a list of error strings
    (empty = valid). Callers should log errors and fall back to defaults
    rather than crash, so the bot keeps running on a bad config file."""
    errors = []
    if not isinstance(cfg, dict):
        return ["strategy must be a JSON object"]
    st = cfg.get("strategy_type")
    if st not in ("exit_pool", "trend_following", "fixed"):
        errors.append(f"strategy_type must be one of exit_pool|trend_following|fixed (got {st!r})")
    params = cfg.get("parameters", {})
    if not isinstance(params, dict):
        errors.append("parameters must be an object")
        params = {}
    bw = params.get("base_width_pct", params.get("width_pct"))
    if bw is not None:
        try:
            if not (1 <= float(bw) <= 100):
                errors.append(f"base_width_pct/width_pct must be in [1,100] (got {bw})")
        except (TypeError, ValueError):
            errors.append(f"base_width_pct/width_pct must be numeric (got {bw!r})")
    for key, lo, hi in (("buffer_pct", 0, 50),
                        ("trend_shift", 0, 2),
                        ("exit_trend_pct", 0, 50),
                        ("enter_trend_pct", 0, 50)):
        v = params.get(key)
        if v is None:
            continue
        try:
            if not (lo <= float(v) <= hi):
                errors.append(f"{key} must be in [{lo},{hi}] (got {v})")
        except (TypeError, ValueError):
            errors.append(f"{key} must be numeric (got {v!r})")
    ds = cfg.get("data_sources", {})
    ind = ds.get("indicators", {}) if isinstance(ds, dict) else {}
    for key in ("ema_fast", "ema_slow", "atr_period", "rsi_period"):
        v = ind.get(key)
        if v is None:
            continue
        if not (isinstance(v, int) and v > 0):
            errors.append(f"indicators.{key} must be a positive integer (got {v!r})")
    return errors


def warm_samples(indicators):
    """Minimum price samples required before indicators are meaningful."""
    return max(int(indicators.get("ema_slow", 50)),
               int(indicators.get("rsi_period", 14)) + 1,
               int(indicators.get("atr_period", 14)) + 1)


def ema(prices, per):
    """EMA seeded with the SMA of the first `per` samples. Returns None if
    we don't yet have `per` samples."""
    if len(prices) < per:
        return None
    k = 2 / (per + 1)
    e = sum(prices[:per]) / per
    for p in prices[per:]:
        e = p * k + e * (1 - k)
    return e


def atr(prices, per):
    """Wilder's ATR: first value = SMA of first `per` true ranges, then
    smoothed by (per-1)/per. Returns None if insufficient samples."""
    if len(prices) < per + 1:
        return None
    trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    a = sum(trs[:per]) / per
    for t in trs[per:]:
        a = (a * (per - 1) + t) / per
    return a


def rsi(prices, per):
    """Standard Wilder's RSI. Returns None if insufficient samples, 100 on
    pure-gain series, 0 on pure-loss series, 50 when no movement."""
    if len(prices) < per + 1:
        return None
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gs = [max(d, 0) for d in diffs]
    ls = [max(-d, 0) for d in diffs]
    ag = sum(gs[:per]) / per
    al = sum(ls[:per]) / per
    for i in range(per, len(diffs)):
        ag = (ag * (per - 1) + gs[i]) / per
        al = (al * (per - 1) + ls[i]) / per
    if al == 0:
        return 100 if ag > 0 else 50
    return 100 - (100 / (1 + ag / al))


def il_estimate(old_lo, old_hi, current_price, position_value):
    """Standard V2 IL formula * bounded V3 concentration amplifier.

    Returns (il_pct, il_usd). Both zero if inputs are non-positive.
    Per-rebalance IL is capped at 20% and the amplifier at 4x to avoid
    runaway estimates when the range is very narrow.
    """
    if old_lo <= 0 or old_hi <= 0 or current_price <= 0 or position_value <= 0:
        return 0.0, 0.0
    entry = (old_lo + old_hi) / 2
    if entry <= 0:
        return 0.0, 0.0
    r = current_price / entry
    il_v2 = abs(2 * math.sqrt(r) / (1 + r) - 1) if r > 0 else 0
    width_pct = (old_hi - old_lo) / entry * 100
    amp = min(math.sqrt(100 / max(width_pct, 5)), 4.0)
    il_pct = min(il_v2 * amp, 0.20)
    return il_pct, il_pct * position_value


def evaluate_strategy(cfg, price, state, price_history):
    """Pure signal engine shared by lp_daemon and the backtest.

    Inputs:
      cfg             strategy config (same shape validated by validate_strategy).
      price           current price (float, USD per ETH).
      state           dict with keys range_lo, range_hi, pool_active, hold_asset.
      price_history   list of floats OR list of {'p': float, ...}.

    Returns (signal, action_or_None, details):
      signal ∈ {gray, green, yellow, rebalance, exit, enter, closed}
      action: when non-None, a dict with keys:
        type ∈ {rebalance, exit_pool, enter_pool}, and one of:
          {lo, hi, width, reason}  for rebalance / enter_pool
          {hold: 'ETH'|'USDC', reason}  for exit_pool
      details: indicator snapshot (trend, trend_pct, rsi, ema_fast/slow, vol, atr).
    """
    p = cfg.get("parameters", {}) if isinstance(cfg, dict) else {}
    st = cfg.get("strategy_type", "exit_pool") if isinstance(cfg, dict) else "exit_pool"
    ic = cfg.get("data_sources", {}).get("indicators", {}) if isinstance(cfg, dict) else {}

    if price_history and isinstance(price_history[0], dict):
        prices = [x["p"] for x in price_history]
    else:
        prices = list(price_history)

    need = warm_samples(ic)
    if len(prices) < need:
        return "gray", None, {"warming_up": True,
                              "message": f"Warming up ({len(prices)}/{need} samples)"}

    ef = ema(prices, int(ic.get("ema_fast", 20)))
    es = ema(prices, int(ic.get("ema_slow", 50)))
    at = atr(prices, int(ic.get("atr_period", 14)))
    rs = rsi(prices, int(ic.get("rsi_period", 14)))
    vp = (at / price * 100) if (at is not None and price > 0) else 0
    tu = (ef is not None and es is not None and ef > es)
    tp = ((ef - es) / es * 100) if (ef is not None and es is not None and es > 0) else 0

    det = {"price": price,
           "trend": "up" if tu else "down",
           "trend_pct": round(tp, 2),
           "ema_fast": round(ef, 2) if ef is not None else None,
           "ema_slow": round(es, 2) if es is not None else None,
           "rsi": round(rs, 1) if rs is not None else None,
           "vol": round(vp, 2),
           "atr": round(at, 2) if at is not None else None}

    rlo = state.get("range_lo", 0) or 0
    rhi = state.get("range_hi", 0) or 0
    pool_active = state.get("pool_active", True)
    hold_asset = state.get("hold_asset")

    if not pool_active:
        nt = p.get("enter_trend_pct", 2)
        bw = p.get("base_width_pct", 15)
        if abs(tp) < nt:
            hw = bw / 200
            ts2 = p.get("trend_shift", 0.4)
            sh = hw * ts2 * min(abs(tp) / 100 * 8, 1)
            nc = price * (1 + sh) if tu else price * (1 - sh)
            return "enter", {"type": "enter_pool",
                             "lo": round(nc * (1 - bw / 200), 2),
                             "hi": round(nc * (1 + bw / 200), 2),
                             "width": bw,
                             "reason": f"Lateralizacion (trend {tp:+.1f}%)"}, det
        h = hold_asset or "USDC"
        if h == "ETH" and rs is not None and rs < 35:
            return "enter", {"type": "enter_pool",
                             "lo": round(price * (1 - bw / 200), 2),
                             "hi": round(price * (1 + bw / 200), 2),
                             "width": bw,
                             "reason": f"RSI {rs:.0f}, reversion"}, det
        if h == "USDC" and rs is not None and rs > 65:
            return "enter", {"type": "enter_pool",
                             "lo": round(price * (1 - bw / 200), 2),
                             "hi": round(price * (1 + bw / 200), 2),
                             "width": bw,
                             "reason": f"RSI {rs:.0f}, reversion"}, det
        return "closed", None, det

    if rlo <= 0 or rhi <= 0:
        det["message"] = "No range set"
        return "gray", None, det

    inr = rlo <= price <= rhi

    if st == "exit_pool" and abs(tp) > p.get("exit_trend_pct", 10):
        h = "ETH" if tp > 0 else "USDC"
        return "exit", {"type": "exit_pool", "hold": h,
                        "reason": f"Tendencia fuerte ({tp:+.1f}%), exit -> {h}"}, det

    buf = p.get("buffer_pct", 5) / 100
    if not inr and (price < rlo * (1 - buf) or price > rhi * (1 + buf)):
        bw = p.get("base_width_pct", 15)
        ts2 = p.get("trend_shift", 0.4)
        hw = bw / 200
        sh = hw * ts2 * min(abs(tp) / 100 * 8, 1)
        nc = price * (1 + sh) if tu else price * (1 - sh)
        return "rebalance", {"type": "rebalance",
                             "lo": round(nc * (1 - bw / 200), 2),
                             "hi": round(nc * (1 + bw / 200), 2),
                             "width": bw,
                             "reason": f"Fuera de rango, trend {tp:+.1f}%"}, det

    if not inr:
        return "yellow", None, det

    rw = rhi - rlo
    edge = min(price - rlo, rhi - price) / rw * 100 if rw > 0 else 0
    det["edge_dist_pct"] = round(edge, 1)
    if edge < 5:
        return "yellow", None, det

    return "green", None, det
