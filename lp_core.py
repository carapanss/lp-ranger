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
