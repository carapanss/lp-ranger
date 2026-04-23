"""Grid search + walk-forward validation over LP strategy configs.

Train/test cadence: for each anchor day t in {180, 210, 240, ..., 335},
train on candles[t-180:t] (grid search by APR), then score the winner
out-of-sample on candles[t:t+30]. The final ranked output is by mean
OOS APR across the 6 test windows — this is the antidote to overfit.
"""

import csv
import json
import time
from dataclasses import dataclass, field, asdict
from itertools import product
from pathlib import Path

import lp_core
from backtest.engine import run_backtest, precompute_indicators

REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "backtest" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Grid definitions ──────────────────────────────────────────────
GRIDS = {
    "exit_pool": {
        "base_width_pct": [8, 12, 15, 18, 25, 35],
        "exit_trend_pct": [5, 7, 10, 15, 20],
        "enter_trend_pct": [1, 2, 3, 5],
        "trend_shift":    [0.2, 0.4, 0.6],
        "buffer_pct":     [2, 5, 10],
    },
    "trend_following": {
        "base_width_pct": [8, 12, 15, 18, 25, 35],
        "trend_shift":    [0.2, 0.35, 0.5, 0.7],
        "buffer_pct":     [2, 5, 10],
    },
    "fixed": {
        "base_width_pct": [5, 8, 10, 12, 15, 18, 25, 35],
        "buffer_pct":     [0, 2, 5],
    },
}

INDICATOR_GRID = {
    "ema_fast":   [10, 20],
    "ema_slow":   [30, 50, 100],
    "atr_period": [14],
    "rsi_period": [14],
}


def _cfgs_for(strategy_type):
    """Yield all cfgs for a given strategy_type.

    The `fixed` strategy does not use any indicators for signal generation,
    so its cfgs are emitted with the default indicator combo only — this
    avoids 6x wasted work and keeps each fixed point unique in the summary.
    """
    pg = GRIDS[strategy_type]
    pkeys = list(pg.keys())
    if strategy_type == "fixed":
        ind_iter = [{"ema_fast": 20, "ema_slow": 50,
                     "atr_period": 14, "rsi_period": 14}]
    else:
        ikeys = list(INDICATOR_GRID.keys())
        ind_iter = [dict(zip(ikeys, ic))
                    for ic in product(*(INDICATOR_GRID[k] for k in ikeys))]
    for pcombo in product(*(pg[k] for k in pkeys)):
        for ind in ind_iter:
            params = dict(zip(pkeys, pcombo))
            cfg = {
                "name": f"grid_{strategy_type}",
                "version": "grid",
                "strategy_type": strategy_type,
                "parameters": params,
                "data_sources": {"indicators": dict(ind)},
            }
            if strategy_type == "fixed":
                cfg["parameters"]["width_pct"] = cfg["parameters"]["base_width_pct"]
            yield cfg


@dataclass
class SearchResult:
    strategy_type: str
    params: dict
    indicators: dict
    train_apr: float = 0.0
    test_apr: float = 0.0
    window_start: int = 0
    test_fees: float = 0.0
    test_il: float = 0.0
    test_gas: float = 0.0
    test_drawdown: float = 0.0
    test_rebalances: int = 0
    test_exits: int = 0


def slice_candles(candles, start_day, end_day):
    """Return candles whose timestamp falls in [start_day, end_day) relative
    to the first candle. Day units are calendar days (86400s)."""
    if not candles:
        return []
    t0 = candles[0]["t"]
    lo = t0 + start_day * 86400000
    hi = t0 + end_day * 86400000
    return [c for c in candles if lo <= c["t"] < hi]


def grid_search_one_window(candles_train):
    """For every cfg in every strategy_type, run train backtest and return
    the top cfg per strategy_type (by net APR).

    Indicators are precomputed once per indicator_combo and shared across
    all param combos — amortises the O(n) sweep, giving ~50x speedup.
    """
    best_by_type = {}
    failures = []
    # Cache indicators by (ema_fast, ema_slow, atr_period, rsi_period)
    ind_cache = {}
    for stype in GRIDS:
        best = None
        for cfg in _cfgs_for(stype):
            ic = cfg["data_sources"]["indicators"]
            key = (ic["ema_fast"], ic["ema_slow"], ic["atr_period"], ic["rsi_period"])
            if key not in ind_cache:
                ind_cache[key] = precompute_indicators(candles_train, ic)
            try:
                r = run_backtest(cfg, candles_train, indicators=ind_cache[key])
            except Exception as e:
                failures.append({
                    "strategy_type": stype,
                    "params": dict(cfg["parameters"]),
                    "indicators": dict(ic),
                    "error": str(e),
                })
                continue
            if best is None or r.net_apr > best[1].net_apr:
                best = (cfg, r)
        if best is not None:
            best_by_type[stype] = best
    return best_by_type, failures


def walk_forward(candles, *, train_days=180, test_days=30, step_days=30):
    """Roll forward through the candle stream. Returns a list[SearchResult],
    one per (strategy_type, test_window)."""
    if not candles:
        return []

    # Total window in days
    total_days = (candles[-1]["t"] - candles[0]["t"]) / 86400000.0
    total_days = int(total_days)

    results: list[SearchResult] = []
    anchors = list(range(train_days, total_days - test_days + 1, step_days))
    print(f"[walk-forward] total={total_days}d | anchors={anchors} | "
          f"windows={len(anchors)} per strategy_type")

    for i, t in enumerate(anchors):
        t0 = time.time()
        c_train = slice_candles(candles, t - train_days, t)
        c_test = slice_candles(candles, t, t + test_days)
        if len(c_train) < 100 or len(c_test) < 50:
            print(f"  window {i+1}/{len(anchors)}: too few candles, skipping")
            continue

        best_by_type, failures = grid_search_one_window(c_train)
        for stype, (cfg, train_r) in best_by_type.items():
            test_r = run_backtest(cfg, c_test)
            results.append(SearchResult(
                strategy_type=stype,
                params=dict(cfg["parameters"]),
                indicators=dict(cfg["data_sources"]["indicators"]),
                train_apr=train_r.net_apr,
                test_apr=test_r.net_apr,
                window_start=c_test[0]["t"] if c_test else 0,
                test_fees=test_r.total_fees_usd,
                test_il=test_r.total_il_usd,
                test_gas=test_r.total_gas_usd,
                test_drawdown=test_r.max_drawdown_pct,
                test_rebalances=test_r.n_rebalances,
                test_exits=test_r.n_exits,
            ))
        fail_note = f" | failed_cfgs={len(failures)}" if failures else ""
        print(f"  window {i+1}/{len(anchors)}: {time.time()-t0:.1f}s{fail_note} | "
              + " | ".join(f"{s}: train={b[1].net_apr:.0f}% test={run_backtest(b[0], c_test).net_apr:.0f}%"
                          for s,b in best_by_type.items()))
    return results


def summarise(results):
    """Per (strategy_type, params) -> mean_oos_apr, stability, etc."""
    buckets = {}
    for r in results:
        key = (r.strategy_type,
               tuple(sorted(r.params.items())),
               tuple(sorted(r.indicators.items())))
        buckets.setdefault(key, []).append(r)
    summary = []
    for (stype, params_tup, ind_tup), rs in buckets.items():
        aprs = [r.test_apr for r in rs]
        mean_apr = sum(aprs) / len(aprs) if aprs else 0
        sorted_aprs = sorted(aprs)
        mid = len(sorted_aprs) // 2
        median_apr = (sorted_aprs[mid] if len(sorted_aprs) % 2 == 1
                      else (sorted_aprs[mid-1] + sorted_aprs[mid]) / 2) if sorted_aprs else 0
        positive = sum(1 for a in aprs if a > 0)
        summary.append({
            "strategy_type": stype,
            "params": dict(params_tup),
            "indicators": dict(ind_tup),
            "n_windows": len(rs),
            "mean_oos_apr": round(mean_apr, 2),
            "median_oos_apr": round(median_apr, 2),
            "positive_windows": positive,
            "avg_drawdown": round(sum(r.test_drawdown for r in rs) / len(rs), 2),
            "avg_fees": round(sum(r.test_fees for r in rs) / len(rs), 2),
            "avg_il": round(sum(r.test_il for r in rs) / len(rs), 2),
        })
    summary.sort(key=lambda s: s["mean_oos_apr"], reverse=True)
    return summary


def write_reports(results, summary, csv_path=None, json_path=None):
    csv_path = Path(csv_path or REPORTS_DIR / "walkforward.csv")
    json_path = Path(json_path or REPORTS_DIR / "summary.json")
    fields = ["strategy_type", "window_start", "train_apr", "test_apr",
              "test_fees", "test_il", "test_gas", "test_drawdown",
              "test_rebalances", "test_exits", "params", "indicators"]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in results:
            w.writerow([getattr(r, k) if k not in ("params", "indicators")
                        else json.dumps(getattr(r, k))
                        for k in fields])
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    return csv_path, json_path


def write_optimal_strategy(winner, path):
    cfg = {
        "name": f"Optimal ({winner['strategy_type']})",
        "version": "1.0",
        "strategy_type": winner["strategy_type"],
        "description": f"Walk-forward winner. Mean OOS APR {winner['mean_oos_apr']}% across {winner['n_windows']} windows.",
        "parameters": dict(winner["params"]),
        "data_sources": {
            "indicators": dict(winner["indicators"]),
            "position_poll_interval_seconds": 900,
        },
        "backtest_results": {
            "mean_oos_apr_pct": winner["mean_oos_apr"],
            "median_oos_apr_pct": winner["median_oos_apr"],
            "positive_windows": f"{winner['positive_windows']}/{winner['n_windows']}",
            "avg_drawdown_pct": winner["avg_drawdown"],
        },
    }
    errs = lp_core.validate_strategy(cfg)
    if errs:
        raise RuntimeError(f"optimal cfg failed validation: {errs}")
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


# ── CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from backtest.data_loader import fetch_eth_usd

    ap = argparse.ArgumentParser()
    ap.add_argument("--single", help="Path to a single strategy JSON to run (no search)")
    ap.add_argument("--walk-forward", action="store_true", help="Run walk-forward grid search")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--step-days", type=int, default=30)
    args = ap.parse_args()

    candles = fetch_eth_usd(days=args.days, cache_path=args.cache)
    print(f"loaded {len(candles)} candles "
          f"from {candles[0]['t']} to {candles[-1]['t']}")

    if args.single:
        cfg = json.load(open(args.single))
        r = run_backtest(cfg, candles)
        print(f"{args.single}: APR={r.net_apr}% fees=${r.total_fees_usd} "
              f"il=${r.total_il_usd} gas=${r.total_gas_usd} "
              f"rebs={r.n_rebalances} exits={r.n_exits} "
              f"in_pool={r.time_in_pool_pct}% in_eth={r.time_in_eth_pct}% "
              f"in_usdc={r.time_in_usdc_pct}% max_dd={r.max_drawdown_pct}%")
    elif args.walk_forward:
        results = walk_forward(candles,
                               train_days=args.train_days,
                               test_days=args.test_days,
                               step_days=args.step_days)
        summary = summarise(results)
        csv_p, json_p = write_reports(results, summary)
        print(f"\nTop 10 by mean OOS APR:")
        for i, s in enumerate(summary[:10]):
            print(f"  {i+1:2d}. {s['strategy_type']:15s} mean={s['mean_oos_apr']:6.1f}% "
                  f"med={s['median_oos_apr']:6.1f}% +w={s['positive_windows']}/{s['n_windows']} "
                  f"dd={s['avg_drawdown']:.1f}% params={s['params']}")
        winner = summary[0]
        opt_path = REPO / "strategy_optimal.json"
        write_optimal_strategy(winner, opt_path)
        print(f"\nWritten {opt_path} (mean OOS APR = {winner['mean_oos_apr']}%)")
        print(f"Reports: {csv_p} | {json_p}")
    else:
        ap.print_help()
