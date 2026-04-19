"""Historical ETH/USD price loader for backtests.

Primary source: Binance ETHUSDT 1h klines (free, no key required).
The ETHUSDT / ETHUSDC basis is < 0.1% over a 1y window — acceptable for
strategy ranking. Data is cached in backtest/data/eth_usd_1y.json and
subsequent calls return offline.

Canonical candle shape:  {"t": unix_ms, "p": close_usd}
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
COINGECKO_CHART = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart"

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = REPO / "backtest" / "data" / "eth_usd_1y.json"

_INTERVAL_MS = {"1h": 3600 * 1000, "4h": 4 * 3600 * 1000, "1d": 86400 * 1000}


def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "lp-ranger-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _binance_page(symbol, interval, start_ms, limit=1000):
    url = (f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&limit={limit}")
    return _http_json(url)


def _fetch_binance(days, interval, symbol="ETHUSDT"):
    if interval not in _INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    step_ms = _INTERVAL_MS[interval]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    out = []
    cursor = start_ms
    while cursor < end_ms:
        rows = _binance_page(symbol, interval, cursor, 1000)
        if not rows:
            break
        for row in rows:
            # row: [open_time, open, high, low, close, volume, close_time, ...]
            out.append({"t": int(row[0]), "p": float(row[4])})
        last_close = int(rows[-1][6])
        cursor = last_close + 1
        if len(rows) < 1000:
            break
        time.sleep(0.2)
    return out


def _fetch_coingecko(days):
    """Fallback: CoinGecko market_chart. At days=365 on the free tier this
    serves daily granularity — coarser than Binance hourly."""
    url = f"{COINGECKO_CHART}?vs_currency=usd&days={days}"
    data = _http_json(url, timeout=30)
    prices = data.get("prices", [])
    return [{"t": int(ts), "p": float(p)} for ts, p in prices]


def fetch_eth_usd(days=365, interval="1h", source="binance",
                  cache_path=None, force_refresh=False):
    """Return a list of {'t': unix_ms, 'p': close_usd} candles.

    Reads from cache on subsequent calls unless force_refresh=True. The
    cache also records the source and interval so switching sources
    implies a refresh.
    """
    cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and not force_refresh:
        try:
            with open(cache_path) as f:
                blob = json.load(f)
            if (blob.get("source") == source
                    and blob.get("interval") == interval
                    and blob.get("days") == days
                    and len(blob.get("candles", [])) > 100):
                return blob["candles"]
        except Exception:
            pass  # fall through to refresh

    if source == "binance":
        candles = _fetch_binance(days, interval)
    elif source == "binance_usdc":
        candles = _fetch_binance(days, interval, symbol="ETHUSDC")
        if len(candles) < days * 20:  # ETHUSDC has gaps on Binance
            candles = _fetch_binance(days, interval, symbol="ETHUSDT")
    elif source == "coingecko":
        candles = _fetch_coingecko(days)
    else:
        raise ValueError(f"unknown source: {source}")

    if not candles:
        raise RuntimeError(f"no candles fetched from {source}")

    # Sort, dedupe on timestamp
    candles.sort(key=lambda c: c["t"])
    dedup = []
    last_t = None
    for c in candles:
        if c["t"] != last_t:
            dedup.append(c)
            last_t = c["t"]

    blob = {"source": source,
            "interval": interval,
            "days": days,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "candles": dedup}
    tmp = cache_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(blob, f)
    os.replace(tmp, cache_path)
    return dedup


def summary(candles):
    if not candles:
        return "empty"
    first = candles[0]
    last = candles[-1]
    lo = min(c["p"] for c in candles)
    hi = max(c["p"] for c in candles)
    days = (last["t"] - first["t"]) / 86400000
    return (f"{len(candles)} candles over {days:.1f}d | "
            f"range ${lo:,.0f}-${hi:,.0f} | "
            f"start ${first['p']:,.0f} -> end ${last['p']:,.0f} "
            f"({(last['p']/first['p']-1)*100:+.1f}%)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--source", default="binance",
                    choices=("binance", "binance_usdc", "coingecko"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    c = fetch_eth_usd(days=args.days, interval=args.interval,
                      source=args.source, force_refresh=args.refresh)
    print(summary(c))
