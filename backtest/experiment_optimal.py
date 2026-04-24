"""Targeted walk-forward search for improving strategy_optimal.json.

Explores two families beyond the baseline fixed-width winner:
  1. fixed + adaptive width + optional in-range recenter
  2. exit_pool + idle lending APR while capital sits outside the pool

The search is narrower than backtest/search.py but more expressive for the
ideas under consideration here.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from itertools import product
from pathlib import Path

from backtest.data_loader import fetch_eth_usd
from backtest.engine import precompute_indicators, run_backtest
from backtest.search import slice_candles

REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "backtest" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

INDICATOR_GRID = {
    "ema_fast": [10, 20],
    "ema_slow": [30],
    "atr_period": [14],
    "rsi_period": [14],
}

FIXED_GRID = {
    "base_width_pct": [3, 4, 5],
    "min_width_pct": [3],
    "max_width_pct": [5, 6],
    "buffer_pct": [0, 1],
    "volatility_width_mult": [0.0, 1.0],
    "rsi_width_mult": [0.0, 0.03],
    "trend_width_mult": [0.0, 0.2],
    "recenter_threshold_pct": [0.0, 1.0],
    "rewidth_threshold_pct": [0.0, 1.0],
}

EXIT_LENDING_GRID = {
    "base_width_pct": [6, 8, 10],
    "exit_trend_pct": [5, 7, 10],
    "enter_trend_pct": [1, 2],
    "trend_shift": [0.2, 0.4],
    "buffer_pct": [2, 5],
    "idle_lend_usdc_apr": [0.0, 4.0, 8.0],
    "idle_lend_eth_apr": [0.0, 1.5],
}


@dataclass
class ExperimentRow:
    family: str
    strategy_type: str
    params: dict
    indicators: dict
    train_apr: float
    test_apr: float
    test_fees: float
    test_il: float
    test_gas: float
    test_drawdown: float
    test_rebalances: int
    test_exits: int
    window_start: int


def _indicator_combos():
    keys = list(INDICATOR_GRID.keys())
    for combo in product(*(INDICATOR_GRID[k] for k in keys)):
        yield dict(zip(keys, combo))


def fixed_candidates():
    keys = list(FIXED_GRID.keys())
    for pcombo in product(*(FIXED_GRID[k] for k in keys)):
        params = dict(zip(keys, pcombo))
        params["width_pct"] = params["base_width_pct"]
        for ind in _indicator_combos():
            yield {
                "family": "fixed_adaptive",
                "name": "optimal_experiment_fixed",
                "version": "exp",
                "strategy_type": "fixed",
                "parameters": params,
                "data_sources": {"indicators": dict(ind)},
            }


def exit_lending_candidates():
    keys = list(EXIT_LENDING_GRID.keys())
    for pcombo in product(*(EXIT_LENDING_GRID[k] for k in keys)):
        params = dict(zip(keys, pcombo))
        for ind in _indicator_combos():
            yield {
                "family": "exit_lending",
                "name": "optimal_experiment_exit_lending",
                "version": "exp",
                "strategy_type": "exit_pool",
                "parameters": params,
                "data_sources": {"indicators": dict(ind)},
            }


def all_candidates():
    yield from fixed_candidates()
    yield from exit_lending_candidates()


def shortlist_candidates(candidates, candles, *, train_days=180, shortlist_per_family=20):
    """Fast first pass: keep the top-N train APR configs per family."""
    c_train = slice_candles(candles, 0, train_days)
    ranked = {}
    ind_cache = {}
    for cfg in candidates:
        ic = cfg["data_sources"]["indicators"]
        key = (ic["ema_fast"], ic["ema_slow"], ic["atr_period"], ic["rsi_period"])
        if key not in ind_cache:
            ind_cache[key] = precompute_indicators(c_train, ic)
        res = run_backtest(cfg, c_train, indicators=ind_cache[key])
        ranked.setdefault(cfg["family"], []).append((res.net_apr, cfg))
    shortlisted = []
    for family, rows in ranked.items():
        rows.sort(key=lambda item: item[0], reverse=True)
        shortlisted.extend(cfg for _, cfg in rows[:shortlist_per_family])
    return shortlisted


def _group_best(rows):
    best = {}
    for cfg, res in rows:
        fam = cfg["family"]
        if fam not in best or res.net_apr > best[fam][1].net_apr:
            best[fam] = (cfg, res)
    return best


def walk_forward_experiment(candles, *, train_days=180, test_days=30, step_days=30):
    total_days = int((candles[-1]["t"] - candles[0]["t"]) / 86400000.0)
    anchors = list(range(train_days, total_days - test_days + 1, step_days))
    candidates = shortlist_candidates(
        list(all_candidates()), candles,
        train_days=train_days, shortlist_per_family=20,
    )
    results: list[ExperimentRow] = []

    print(f"[experiment] candidates={len(candidates)} anchors={anchors}")

    for i, anchor in enumerate(anchors):
        t0 = time.time()
        c_train = slice_candles(candles, anchor - train_days, anchor)
        c_test = slice_candles(candles, anchor, anchor + test_days)
        if len(c_train) < 100 or len(c_test) < 50:
            continue

        ind_cache_train = {}
        ind_cache_test = {}
        train_rows = []
        for cfg in candidates:
            ic = cfg["data_sources"]["indicators"]
            key = (ic["ema_fast"], ic["ema_slow"], ic["atr_period"], ic["rsi_period"])
            if key not in ind_cache_train:
                ind_cache_train[key] = precompute_indicators(c_train, ic)
                ind_cache_test[key] = precompute_indicators(c_test, ic)
            res = run_backtest(cfg, c_train, indicators=ind_cache_train[key])
            train_rows.append((cfg, res))

        best_by_family = _group_best(train_rows)
        for family, (cfg, train_res) in best_by_family.items():
            ic = cfg["data_sources"]["indicators"]
            key = (ic["ema_fast"], ic["ema_slow"], ic["atr_period"], ic["rsi_period"])
            test_res = run_backtest(cfg, c_test, indicators=ind_cache_test[key])
            results.append(ExperimentRow(
                family=family,
                strategy_type=cfg["strategy_type"],
                params=dict(cfg["parameters"]),
                indicators=dict(ic),
                train_apr=train_res.net_apr,
                test_apr=test_res.net_apr,
                test_fees=test_res.total_fees_usd,
                test_il=test_res.total_il_usd,
                test_gas=test_res.total_gas_usd,
                test_drawdown=test_res.max_drawdown_pct,
                test_rebalances=test_res.n_rebalances,
                test_exits=test_res.n_exits,
                window_start=c_test[0]["t"],
            ))
        best_note = " | ".join(
            f"{fam}: train={cfg_res[1].net_apr:.1f}%"
            for fam, cfg_res in best_by_family.items()
        )
        print(f"  window {i+1}/{len(anchors)}: {time.time()-t0:.1f}s | {best_note}")
    return results


def summarise(rows: list[ExperimentRow]):
    buckets = {}
    for row in rows:
        key = (
            row.family,
            row.strategy_type,
            tuple(sorted(row.params.items())),
            tuple(sorted(row.indicators.items())),
        )
        buckets.setdefault(key, []).append(row)

    summary = []
    for (family, strategy_type, params_tup, ind_tup), rs in buckets.items():
        aprs = [r.test_apr for r in rs]
        aprs_sorted = sorted(aprs)
        mid = len(aprs_sorted) // 2
        median = (aprs_sorted[mid] if len(aprs_sorted) % 2
                  else (aprs_sorted[mid - 1] + aprs_sorted[mid]) / 2)
        summary.append({
            "family": family,
            "strategy_type": strategy_type,
            "params": dict(params_tup),
            "indicators": dict(ind_tup),
            "n_windows": len(rs),
            "mean_oos_apr": round(sum(aprs) / len(aprs), 2),
            "median_oos_apr": round(median, 2),
            "positive_windows": sum(1 for a in aprs if a > 0),
            "avg_drawdown": round(sum(r.test_drawdown for r in rs) / len(rs), 2),
            "avg_fees": round(sum(r.test_fees for r in rs) / len(rs), 2),
            "avg_il": round(sum(r.test_il for r in rs) / len(rs), 2),
            "avg_gas": round(sum(r.test_gas for r in rs) / len(rs), 2),
            "avg_rebalances": round(sum(r.test_rebalances for r in rs) / len(rs), 2),
            "avg_exits": round(sum(r.test_exits for r in rs) / len(rs), 2),
        })
    summary.sort(key=lambda r: (r["mean_oos_apr"], -r["avg_il"]), reverse=True)
    return summary


def write_reports(rows, summary):
    csv_path = REPORTS_DIR / "optimal_experiment_walkforward.csv"
    json_path = REPORTS_DIR / "optimal_experiment_summary.json"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "family", "strategy_type", "window_start", "train_apr", "test_apr",
            "test_fees", "test_il", "test_gas", "test_drawdown",
            "test_rebalances", "test_exits", "params", "indicators",
        ])
        for row in rows:
            w.writerow([
                row.family, row.strategy_type, row.window_start,
                row.train_apr, row.test_apr, row.test_fees, row.test_il,
                row.test_gas, row.test_drawdown, row.test_rebalances,
                row.test_exits, json.dumps(row.params), json.dumps(row.indicators),
            ])
    with json_path.open("w") as f:
        json.dump(summary, f, indent=2)
    return csv_path, json_path


def write_optimal_from_summary(winner):
    cfg = {
        "name": f"Optimal ({winner['family']})",
        "version": "1.1",
        "strategy_type": winner["strategy_type"],
        "description": (
            f"Walk-forward winner from experimental search. Mean OOS APR "
            f"{winner['mean_oos_apr']}% across {winner['n_windows']} windows."
        ),
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
            "avg_fees_usd": winner["avg_fees"],
            "avg_il_usd": winner["avg_il"],
            "avg_gas_usd": winner["avg_gas"],
            "avg_rebalances": winner["avg_rebalances"],
            "avg_exits": winner["avg_exits"],
            "family": winner["family"],
        },
    }
    out = REPO / "strategy_optimal.json"
    with out.open("w") as f:
        json.dump(cfg, f, indent=2)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--step-days", type=int, default=30)
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()

    candles = fetch_eth_usd(days=args.days, cache_path=args.cache)
    rows = walk_forward_experiment(
        candles,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
    )
    summary = summarise(rows)
    csv_path, json_path = write_reports(rows, summary)
    print("\nTop candidates:")
    for i, row in enumerate(summary[:10], start=1):
        print(
            f"  {i:2d}. {row['family']:14s} mean={row['mean_oos_apr']:6.1f}% "
            f"med={row['median_oos_apr']:6.1f}% dd={row['avg_drawdown']:.2f}% "
            f"il=${row['avg_il']:.2f} gas=${row['avg_gas']:.2f} "
            f"params={row['params']}"
        )
    out = write_optimal_from_summary(summary[0])
    print(f"\nWritten {out}")
    print(f"Reports: {csv_path} | {json_path}")
