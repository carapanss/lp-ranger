#!/usr/bin/env python3
"""
LP Ranger web dashboard.

Stdlib-only HTTP server that exposes a read-only view of every lp-daemon
discovered under /var/lib/lp-ranger/bots/<position_id>/.local/share/lp-ranger/,
plus wallet-based position discovery and bot start/stop controls.

Design constraints:
- < 40 MB RSS on a Pi Zero 2W.
- No extra pip deps; only Python standard library.
- Stateless for state reads: every request re-reads from disk.
- Positions endpoint caches per wallet for 60 s to limit RPC load.
- Controls run narrowly-scoped systemctl via a dedicated sudoers drop-in.
"""
import json
import hmac
import math
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# --- Config ---
HOST = "0.0.0.0"
PORT = 8080
BOTS_DIR = Path("/var/lib/lp-ranger/bots")
USER_STRATEGIES_DIR = Path("/var/lib/lp-ranger/strategies")
WEB_STATE_DIR = Path("/var/lib/lp-ranger/web")
WEB_PASSWORD_FILE = WEB_STATE_DIR / "password"
APP_DIR = Path("/opt/lp-ranger")
INDEX_FILE = Path(__file__).parent / "index.html"

MAX_STRATEGY_BYTES = 16 * 1024  # strategy JSONs are ~1-2 KB; 16 KB is a lot of room
STRATEGY_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,40}$")

STALE_AFTER_SECONDS = 15 * 60
LOG_TAIL_BYTES = 16 * 1024
LOG_TAIL_LINES = 200

# Base chain (chain id 8453) Uniswap V3 NonfungiblePositionManager and token addrs.
# mainnet.base.org rate-limits aggressively (~2 req/s) — fine for the daemon
# (1 req/15 min) but too tight for enumerating N positions on demand. llamarpc
# has no such limit on casual use. Fallbacks cover the unusual case it's down.
BASE_RPC_ENDPOINTS = [
    "https://base.llamarpc.com",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://mainnet.base.org",  # last resort: slow but authoritative
]
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Strategies come from two places:
#   - Built-in: /opt/lp-ranger/strategy_<name>.json  (shipped by upstream, read-only)
#   - User:     /var/lib/lp-ranger/strategies/<name>.json  (uploaded via the web UI)
# User strategies take precedence if names collide.

POSITIONS_CACHE_TTL = 60
_positions_cache: dict[str, tuple[float, list]] = {}

_start_time = time.time()
_strategy_perf_lock = threading.Lock()


# --- Bot state (local filesystem) ---

ERROR_RE = re.compile(r"\[ERROR\]|FATAL")


def auth_enabled() -> bool:
    try:
        return bool(WEB_PASSWORD_FILE.read_text().strip())
    except OSError:
        return False


def _auth_password() -> str | None:
    try:
        password = WEB_PASSWORD_FILE.read_text().strip()
    except OSError:
        return None
    return password or None

def discover_bots() -> list[str]:
    if not BOTS_DIR.exists():
        return []
    out = []
    for d in sorted(BOTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        data = d / ".local" / "share" / "lp-ranger"
        if (data / "daemon_state.json").exists() or (data / "daemon.log").exists():
            out.append(d.name)
    return out


def tail_log(path: Path) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    if size > LOG_TAIL_BYTES and "\n" in data:
        data = data.split("\n", 1)[1]
    return data.splitlines()[-LOG_TAIL_LINES:]


def _ack_file(data_dir: Path) -> Path:
    return data_dir / "web_error_ack.json"


def latest_error_info(lines: list[str]) -> dict | None:
    error_lines = [line for line in lines if ERROR_RE.search(line)]
    if not error_lines:
        return None
    latest = error_lines[-1]
    same_text_count = sum(1 for line in error_lines if line == latest)
    return {
        "line": latest,
        "count": same_text_count,
    }


def read_error_ack(data_dir: Path) -> dict | None:
    try:
        with _ack_file(data_dir).open() as f:
            ack = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(ack, dict):
        return None
    return ack


def write_error_ack(data_dir: Path, error_info: dict) -> None:
    ack_path = _ack_file(data_dir)
    ack_path.write_text(json.dumps({
        "line": error_info["line"],
        "count": error_info["count"],
        "acked_at": int(time.time()),
    }))


def clear_bot_logs(bot_id: str) -> tuple[bool, str]:
    data_dir = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger"
    if not data_dir.is_dir():
        return False, "bot not found"
    cleared = []
    for name in ("daemon.log", "errors.log", "error.flag"):
        path = data_dir / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w"):
                pass
            cleared.append(name)
        except OSError as e:
            return False, f"clear {name} failed: {e}"
    try:
        _ack_file(data_dir).unlink(missing_ok=True)
    except OSError:
        pass
    return True, f"cleared: {', '.join(cleared)}"


def _bot_config_file(bot_id: str) -> Path:
    return BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger" / "bot_config.json"


def read_bot_config(bot_id: str) -> dict:
    try:
        with _bot_config_file(bot_id).open() as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_bot_config(bot_id: str, config: dict) -> tuple[bool, str]:
    path = _bot_config_file(bot_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2))
        return True, str(path)
    except OSError as e:
        return False, f"write failed: {e}"


def _bot_strategy_name_file(bot_id: str) -> Path:
    return BOTS_DIR / bot_id / "strategy_name"


def _bot_strategy_perf_file(bot_id: str) -> Path:
    return BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger" / "strategy_performance.json"


def _match_strategy_name(content: dict) -> str | None:
    """Resolve a bot strategy JSON to one of the UI's canonical strategy names."""
    if not isinstance(content, dict):
        return None

    known = {s["name"] for s in list_strategies()}
    for key in ("selected_name", "name", "strategy_type"):
        value = content.get(key)
        if isinstance(value, str) and value in known:
            return value

    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    for strategy in list_strategies():
        resolved = read_strategy(strategy["name"])
        if not resolved:
            continue
        candidate = resolved.get("content")
        if not isinstance(candidate, dict):
            continue
        candidate_encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        if candidate_encoded == encoded:
            return strategy["name"]

    return None


def bot_strategy_name(bot_id: str) -> str | None:
    try:
        saved = _bot_strategy_name_file(bot_id).read_text().strip()
    except OSError:
        saved = ""
    if saved:
        known = {s["name"] for s in list_strategies()}
        if saved in known:
            return saved

    strategy_file = BOTS_DIR / bot_id / "strategy.json"
    try:
        with strategy_file.open() as f:
            content = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return _match_strategy_name(content)


def _tracking_days(state: dict | None, state_file: Path) -> float | None:
    if not isinstance(state, dict):
        return None
    started = state.get("tracking_started_at")
    if not started:
        try:
            started = state_file.stat().st_mtime if state_file.exists() else 0
        except OSError:
            started = 0
    if not started:
        return None
    return max(1 / 24, (time.time() - float(started)) / 86400)


def performance_summary(state: dict | None, state_file: Path) -> dict | None:
    if not isinstance(state, dict):
        return None
    fees = float(state.get("total_fees", 0) or 0)
    il = float(state.get("total_il", 0) or 0)
    tracked_days = _tracking_days(state, state_file)
    summary = {
        "fees_usd": round(fees, 2),
        "il_usd": round(il, 2),
        "pnl_usd": round(fees - il, 2),
        "tracked_days": round(tracked_days, 2) if tracked_days is not None else None,
        "tracking_started_at": state.get("tracking_started_at") or None,
    }
    return summary


def _state_position_value_usd(state: dict | None) -> float | None:
    if not isinstance(state, dict):
        return None
    liq = int(state.get("position_liquidity", 0) or 0)
    tick_lo = state.get("position_tick_lower")
    tick_hi = state.get("position_tick_upper")
    price = _state_price_usd(state)
    if not liq or tick_lo is None or tick_hi is None or price is None:
        return None
    return _position_value_usd_from_parts(liq, tick_lo, tick_hi, price)


def _state_price_usd(state: dict | None) -> float | None:
    if not isinstance(state, dict):
        return None
    history = state.get("price_history") or []
    if not history:
        return None
    try:
        return float(history[-1]["p"])
    except (KeyError, TypeError, ValueError):
        return None


def _position_value_usd_from_parts(liq: int, tick_lo: int, tick_hi: int, price: float) -> float | None:
    if price <= 0:
        return None
    tick_lo = int(tick_lo)
    tick_hi = int(tick_hi)
    if tick_lo > tick_hi:
        tick_lo, tick_hi = tick_hi, tick_lo
    raw = price / 1e12
    sqrt_p = math.sqrt(raw)
    sqrt_a = 1.0001 ** (tick_lo / 2)
    sqrt_b = 1.0001 ** (tick_hi / 2)
    liquidity = float(liq)
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
    return weth * price + usdc


def _load_strategy_performance(bot_id: str) -> dict:
    path = _bot_strategy_perf_file(bot_id)
    try:
        with path.open() as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("entries", {})
    data.setdefault("current", None)
    return data


def _save_strategy_performance(bot_id: str, data: dict) -> None:
    path = _bot_strategy_perf_file(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _ensure_strategy_entry(entries: dict, strategy: str) -> dict:
    entry = entries.get(strategy)
    if not isinstance(entry, dict):
        entry = {}
        entries[strategy] = entry
    entry.setdefault("fees_usd", 0.0)
    entry.setdefault("il_usd", 0.0)
    entry.setdefault("tracked_seconds", 0.0)
    entry.setdefault("sessions", 0)
    entry.setdefault("last_capital_usd", None)
    entry.setdefault("cash_flows", [])
    entry.setdefault("cash_in_usd", 0.0)
    entry.setdefault("cash_out_usd", 0.0)
    entry.setdefault("twr_factor", 1.0)
    return entry


def _record_cash_flow(entry: dict, now: float, amount_usd: float, kind: str, note: str) -> None:
    flow = {
        "ts": int(now),
        "amount_usd": round(amount_usd, 2),
        "kind": kind,
        "note": note,
    }
    flows = entry.setdefault("cash_flows", [])
    flows.append(flow)
    entry["cash_flows"] = flows[-100:]
    if amount_usd >= 0:
        entry["cash_in_usd"] += amount_usd
    else:
        entry["cash_out_usd"] += abs(amount_usd)


def _position_snapshot(state: dict | None) -> dict:
    if not isinstance(state, dict):
        return {"position_id": None, "liquidity": 0, "tick_lower": None, "tick_upper": None, "price": None, "capital_usd": None}
    snap = {
        "position_id": str(state.get("position_id") or ""),
        "liquidity": int(state.get("position_liquidity", 0) or 0),
        "tick_lower": state.get("position_tick_lower"),
        "tick_upper": state.get("position_tick_upper"),
        "price": _state_price_usd(state),
    }
    if snap["liquidity"] and snap["tick_lower"] is not None and snap["tick_upper"] is not None and snap["price"] is not None:
        snap["capital_usd"] = _position_value_usd_from_parts(snap["liquidity"], snap["tick_lower"], snap["tick_upper"], snap["price"])
    else:
        snap["capital_usd"] = None
    return snap


def _cash_flow_delta(current: dict, snapshot: dict) -> tuple[float | None, str | None]:
    if not current or not snapshot:
        return None, None
    prev_pid = str(current.get("position_id") or "")
    cur_pid = str(snapshot.get("position_id") or "")
    prev_liq = int(current.get("position_liquidity", 0) or 0)
    cur_liq = int(snapshot.get("liquidity", 0) or 0)
    prev_tick_lo = current.get("position_tick_lower")
    prev_tick_hi = current.get("position_tick_upper")
    cur_tick_lo = snapshot.get("tick_lower")
    cur_tick_hi = snapshot.get("tick_upper")
    cur_price = snapshot.get("price")
    cur_capital = snapshot.get("capital_usd")
    if cur_price is None or cur_capital is None:
        return None, None
    if (
        prev_pid
        and cur_pid
        and prev_pid == cur_pid
        and prev_liq != cur_liq
        and prev_liq > 0
        and prev_tick_lo is not None
        and prev_tick_hi is not None
    ):
        prev_capital_now = _position_value_usd_from_parts(prev_liq, prev_tick_lo, prev_tick_hi, cur_price)
        if prev_capital_now is None:
            return None, None
        delta = cur_capital - prev_capital_now
        if abs(delta) >= max(5.0, prev_capital_now * 0.01):
            return delta, "liquidity_change"
    return None, None


def _apply_return_segment(entry: dict, current: dict, now: float, net_pnl_delta: float) -> None:
    last_update_at = float(current.get("last_update_at", now) or now)
    dt = max(0.0, now - last_update_at)
    base_capital = float(current.get("base_capital_usd", 0) or 0)
    if dt > 0:
        entry["tracked_seconds"] += dt
    if base_capital > 0 and dt > 0:
        segment_return = net_pnl_delta / base_capital
        entry["twr_factor"] *= (1.0 + segment_return)
        entry["last_capital_usd"] = round(base_capital, 2)


def _finalize_strategy_snapshot(data: dict, state: dict | None, now: float, close_session: bool = False) -> None:
    current = data.get("current")
    if not isinstance(current, dict):
        return
    strategy = current.get("strategy")
    if not strategy or not isinstance(state, dict):
        return
    entry = _ensure_strategy_entry(data["entries"], strategy)
    fees_now = float(state.get("total_fees", 0) or 0)
    il_now = float(state.get("total_il", 0) or 0)
    prev_fees = float(current.get("last_fees_usd", fees_now) or fees_now)
    prev_il = float(current.get("last_il_usd", il_now) or il_now)
    fees_delta = fees_now - prev_fees
    il_delta = il_now - prev_il
    if fees_delta >= 0:
        entry["fees_usd"] += fees_delta
    else:
        fees_delta = 0.0
    if il_delta >= 0:
        entry["il_usd"] += il_delta
    else:
        il_delta = 0.0
    net_pnl_delta = fees_delta - il_delta
    snapshot = _position_snapshot(state)
    flow_delta, flow_note = _cash_flow_delta(current, snapshot)
    if flow_delta is not None:
        _apply_return_segment(entry, current, now, net_pnl_delta)
        _record_cash_flow(entry, now, flow_delta, "in" if flow_delta > 0 else "out", flow_note or "cash_flow")
        current["base_capital_usd"] = max(0.0, float(current.get("base_capital_usd", 0) or 0) + flow_delta)
        net_pnl_delta = 0.0
    _apply_return_segment(entry, current, now, net_pnl_delta)
    current["base_capital_usd"] = snapshot.get("capital_usd")
    current["last_update_at"] = now
    current["last_fees_usd"] = fees_now
    current["last_il_usd"] = il_now
    current["last_capital_usd"] = snapshot.get("capital_usd")
    current["position_id"] = snapshot.get("position_id")
    current["position_liquidity"] = snapshot.get("liquidity")
    current["position_tick_lower"] = snapshot.get("tick_lower")
    current["position_tick_upper"] = snapshot.get("tick_upper")


def _activate_strategy(data: dict, strategy: str, state: dict | None, now: float) -> None:
    entry = _ensure_strategy_entry(data["entries"], strategy)
    entry["sessions"] += 1
    fees_now = float((state or {}).get("total_fees", 0) or 0)
    il_now = float((state or {}).get("total_il", 0) or 0)
    snapshot = _position_snapshot(state)
    data["current"] = {
        "strategy": strategy,
        "started_at": now,
        "last_update_at": now,
        "last_fees_usd": fees_now,
        "last_il_usd": il_now,
        "last_capital_usd": snapshot.get("capital_usd"),
        "base_capital_usd": snapshot.get("capital_usd"),
        "position_id": snapshot.get("position_id"),
        "position_liquidity": snapshot.get("liquidity"),
        "position_tick_lower": snapshot.get("tick_lower"),
        "position_tick_upper": snapshot.get("tick_upper"),
    }


def update_strategy_performance(bot_id: str, state: dict | None, strategy_name: str | None) -> list[dict]:
    with _strategy_perf_lock:
        data = _load_strategy_performance(bot_id)
        now = time.time()
        current = data.get("current")
        if isinstance(current, dict):
            _finalize_strategy_snapshot(data, state, now)
        if strategy_name:
            current = data.get("current")
            if not isinstance(current, dict) or current.get("strategy") != strategy_name:
                _activate_strategy(data, strategy_name, state, now)
        _save_strategy_performance(bot_id, data)

        rows: list[dict] = []
        for strategy, entry in sorted(data["entries"].items()):
            fees = float(entry.get("fees_usd", 0) or 0)
            il = float(entry.get("il_usd", 0) or 0)
            tracked_seconds = float(entry.get("tracked_seconds", 0) or 0)
            twr_factor = float(entry.get("twr_factor", 1.0) or 1.0)
            apr_pct = None
            if tracked_seconds > 0 and twr_factor > 0:
                apr_pct = ((twr_factor ** (365 * 86400 / tracked_seconds)) - 1.0) * 100
            rows.append({
                "strategy": strategy,
                "fees_usd": round(fees, 2),
                "il_usd": round(il, 2),
                "pnl_usd": round(fees - il, 2),
                "tracked_days": round(tracked_seconds / 86400, 2) if tracked_seconds > 0 else 0,
                "sessions": int(entry.get("sessions", 0) or 0),
                "last_capital_usd": entry.get("last_capital_usd"),
                "cash_in_usd": round(float(entry.get("cash_in_usd", 0) or 0), 2),
                "cash_out_usd": round(float(entry.get("cash_out_usd", 0) or 0), 2),
                "cash_flow_count": len(entry.get("cash_flows", []) or []),
                "twr_pct": round((twr_factor - 1.0) * 100, 2),
                "apr_pct": round(apr_pct, 2) if apr_pct is not None else None,
                "is_current": bool(isinstance(data.get("current"), dict) and data["current"].get("strategy") == strategy),
            })
        rows.sort(key=lambda row: (row["apr_pct"] is None, -(row["apr_pct"] or 0), -(row["twr_pct"] or 0)))
        return rows


def close_strategy_performance_session(bot_id: str, state: dict | None) -> None:
    with _strategy_perf_lock:
        data = _load_strategy_performance(bot_id)
        if isinstance(data.get("current"), dict):
            _finalize_strategy_snapshot(data, state, time.time(), close_session=True)
            data["current"] = None
            _save_strategy_performance(bot_id, data)


def diagnostics_text(bot_id: str) -> str:
    payload = read_state(bot_id)
    state = payload.get("state") or {}
    data_dir = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger"
    errors_tail = tail_log(data_dir / "errors.log")
    parts = [
        f"bot_id: {bot_id}",
        f"generated_at: {int(time.time())}",
        f"stale: {payload.get('stale')}",
        f"log_last_modified: {payload.get('log_last_modified')}",
        f"has_unseen_error: {payload.get('has_unseen_error')}",
        f"latest_error: {(payload.get('latest_error') or {}).get('line', '')}",
        f"pool_active: {state.get('pool_active')}",
        f"hold_asset: {state.get('hold_asset')}",
        f"range_lo: {state.get('range_lo')}",
        f"range_hi: {state.get('range_hi')}",
        f"last_action_time: {state.get('last_action_time')}",
        f"actions_today: {state.get('actions_today')}",
        "",
        "=== daemon.log tail ===",
        *payload.get("log_tail", []),
        "",
        "=== errors.log tail ===",
        *errors_tail,
    ]
    return "\n".join(parts).encode("utf-8", errors="replace")


def _restart_web_service_async() -> None:
    def _restart():
        time.sleep(1.0)
        subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "lp-web.service"],
            capture_output=True, text=True, timeout=30,
        )
    threading.Thread(target=_restart, daemon=True, name="lp-web-restart").start()


def read_state(bot_id: str) -> dict:
    data_dir = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger"
    state_file = data_dir / "daemon_state.json"
    log_file = data_dir / "daemon.log"

    state = None
    try:
        with state_file.open() as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    log_mtime = None
    stale = True
    try:
        log_mtime = log_file.stat().st_mtime
        stale = (time.time() - log_mtime) > STALE_AFTER_SECONDS
    except FileNotFoundError:
        pass

    log_tail = tail_log(log_file)
    latest_error = latest_error_info(log_tail)
    ack = read_error_ack(data_dir)
    error_acknowledged = bool(
        latest_error
        and ack
        and ack.get("line") == latest_error["line"]
        and int(ack.get("count", -1)) == latest_error["count"]
    )

    strategy_name = bot_strategy_name(bot_id)
    strategy_performance = update_strategy_performance(bot_id, state, strategy_name)

    return {
        "position_id": bot_id,
        "state": state,
        "log_tail": log_tail,
        "log_last_modified": log_mtime,
        "stale": stale,
        "strategy_name": strategy_name,
        "performance": performance_summary(state, state_file),
        "strategy_performance": strategy_performance,
        "latest_error": latest_error,
        "error_acknowledged": error_acknowledged,
        "has_unseen_error": bool(latest_error and not error_acknowledged),
        "bot_config": read_bot_config(bot_id),
    }


def _read_bot_state_file(bot_id: str) -> dict | None:
    state_file = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger" / "daemon_state.json"
    try:
        with state_file.open() as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return state if isinstance(state, dict) else None


# --- Base RPC: raw JSON-RPC, no web3 import (keeps RSS low) ---

class RPCHTTPError(Exception):
    def __init__(self, code: int, body: str = ""):
        super().__init__(f"HTTP {code} {body[:120]}")
        self.code = code
        self.body = body


# A browser-ish UA keeps Cloudflare-fronted RPCs happy.
_RPC_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 lp-web/1.0",
}


def _rpc_call_to(endpoint: str, method: str, params: list) -> dict:
    import urllib.error
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(endpoint, data=payload, headers=_RPC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:200]
        except Exception:
            body = ""
        raise RPCHTTPError(e.code, body) from None


def _eth_call(to: str, data: str) -> str:
    """Try endpoints in order until one succeeds.

    Rate-limit / transient errors → try the next endpoint. This gives us
    low-latency responses from the first working provider and a clean
    fallback when one is degraded.
    """
    last_err: Exception | None = None
    for endpoint in BASE_RPC_ENDPOINTS:
        try:
            res = _rpc_call_to(endpoint, "eth_call", [{"to": to, "data": data}, "latest"])
            if "error" in res:
                # Provider-level error (e.g. rate-limit returned as JSON-RPC error on the batch)
                last_err = RuntimeError(f"{endpoint}: {res['error']}")
                continue
            return res.get("result", "0x")
        except (RPCHTTPError, OSError, json.JSONDecodeError) as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise RuntimeError("no RPC endpoints configured")


def _encode_addr(addr: str) -> str:
    return addr.lower().removeprefix("0x").zfill(64)


def _encode_uint(n: int) -> str:
    return format(n, "x").zfill(64)


def _decode_signed24(hex_slice: str) -> int:
    # Tick is int24 but ABI-encoded into a 32-byte slot, sign-extended.
    n = int(hex_slice, 16)
    if n >= 2 ** 255:
        n -= 2 ** 256
    return n


def _is_valid_wallet(s: str) -> bool:
    if not isinstance(s, str) or not s.startswith("0x") or len(s) != 42:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def positions_for_wallet(wallet: str) -> list[dict]:
    """Enumerate WETH/USDC Uniswap V3 positions owned by `wallet` on Base.

    Matches the daemon's own raw-RPC approach (no web3 import), so this
    endpoint doesn't inflate the web UI's RSS.
    """
    wallet_lower = wallet.lower()
    now = time.time()
    cached = _positions_cache.get(wallet_lower)
    if cached and cached[0] > now:
        return cached[1]

    # balanceOf(address)
    data = "0x70a08231" + _encode_addr(wallet)
    result = _eth_call(POSITION_MANAGER, data)
    count = int(result, 16) if result not in ("", "0x") else 0

    positions: list[dict] = []
    weth_lo = WETH.lower()
    usdc_lo = USDC.lower()

    for i in range(count):
        # tokenOfOwnerByIndex(address, uint256)
        data = "0x2f745c59" + _encode_addr(wallet) + _encode_uint(i)
        res = _eth_call(POSITION_MANAGER, data)
        if not res or res == "0x":
            continue
        token_id = int(res, 16)

        # positions(uint256)
        data = "0x99fbab88" + _encode_uint(token_id)
        res = _eth_call(POSITION_MANAGER, data)
        if not res or len(res) < 2 + 64 * 12:
            continue
        raw = res[2:]
        fields = [raw[j * 64:(j + 1) * 64] for j in range(12)]

        token0 = "0x" + fields[2][-40:].lower()
        token1 = "0x" + fields[3][-40:].lower()
        pair_ok = (
            (token0 == weth_lo and token1 == usdc_lo)
            or (token0 == usdc_lo and token1 == weth_lo)
        )
        if not pair_ok:
            continue

        fee = int(fields[4], 16)
        tick_lower = _decode_signed24(fields[5])
        tick_upper = _decode_signed24(fields[6])
        liquidity = int(fields[7], 16)

        # price_usdc_per_eth = 1.0001^tick * 10^12  (from daemon docs)
        pl = 1.0001 ** tick_lower * 1e12
        pu = 1.0001 ** tick_upper * 1e12
        if pl > pu:
            pl, pu = pu, pl

        positions.append({
            "id": str(token_id),
            "fee": fee,
            "liquidity": str(liquidity),
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "lo": round(pl, 2),
            "hi": round(pu, 2),
            "has_liquidity": liquidity > 0,
        })

    _positions_cache[wallet_lower] = (now + POSITIONS_CACHE_TTL, positions)
    return positions


# --- Systemctl control via narrow sudoers ---

def _systemctl(action: str, bot_id: str) -> tuple[bool, str]:
    """action ∈ {'enable-now', 'disable-now', 'restart'}."""
    if action == "enable-now":
        argv = ["sudo", "-n", "/usr/bin/systemctl", "enable", "--now", f"lp-daemon@{bot_id}.service"]
    elif action == "disable-now":
        argv = ["sudo", "-n", "/usr/bin/systemctl", "disable", "--now", f"lp-daemon@{bot_id}.service"]
    elif action == "restart":
        argv = ["sudo", "-n", "/usr/bin/systemctl", "restart", f"lp-daemon@{bot_id}.service"]
    else:
        return False, f"unknown action {action}"
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    return False, (proc.stderr or proc.stdout or "").strip()


# --- Git / update helpers ---

GIT_BIN = "/usr/bin/git"


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT_BIN, "-C", str(APP_DIR), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_commit_line(line: str) -> dict | None:
    parts = line.split("\x1f")
    if len(parts) != 5:
        return None
    return {"sha": parts[0], "short": parts[1], "message": parts[2], "date": parts[3], "author": parts[4]}


def git_status() -> dict:
    """Local git state + how far behind upstream (no fetch)."""
    def run_ok(*args: str) -> str | None:
        r = _git(*args)
        return r.stdout.strip() if r.returncode == 0 else None

    head = run_ok("rev-parse", "HEAD")
    short = run_ok("rev-parse", "--short", "HEAD")
    branch = run_ok("rev-parse", "--abbrev-ref", "HEAD")
    upstream = run_ok("rev-parse", "--abbrev-ref", "@{u}")
    last = run_ok("log", "-1", "--format=%H\x1f%h\x1f%s\x1f%ai\x1f%an")

    # Only tracked-file modifications count as "dirty" — our web/ dir is
    # untracked and must not block a pull.
    mods = _git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
    mods = [m.strip() for m in mods if m.strip()]

    behind = 0
    if upstream:
        r = _git("rev-list", "--count", "HEAD..@{u}")
        if r.returncode == 0:
            try:
                behind = int(r.stdout.strip())
            except ValueError:
                pass

    return {
        "sha": head,
        "short": short,
        "branch": branch,
        "upstream": upstream,
        "last_commit": _parse_commit_line(last) if last else None,
        "has_local_changes": bool(mods),
        "local_changes": mods,
        "behind_by": behind,
    }


def git_check_updates() -> dict:
    """Run git fetch, then return status + the list of commits we're behind."""
    fetch = _git("fetch", "--quiet", timeout=60)
    if fetch.returncode != 0:
        raise RuntimeError((fetch.stderr or fetch.stdout).strip() or "git fetch failed")
    status = git_status()
    commits: list[dict] = []
    if status["behind_by"] > 0:
        r = _git("log", "HEAD..@{u}", "--format=%H\x1f%h\x1f%s\x1f%ai\x1f%an")
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                c = _parse_commit_line(line)
                if c:
                    commits.append(c)
    status["commits"] = commits
    return status


def git_install() -> dict:
    """Fast-forward pull. Refuses to merge or rebase — safest on a server."""
    r = _git("pull", "--ff-only", timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "git pull failed")
    return git_status()


# --- Strategies ---

def _strategy_path(name: str) -> Path | None:
    """Resolve a strategy name to a file path, preferring user > built-in."""
    if not STRATEGY_NAME_RE.match(name):
        return None
    user = USER_STRATEGIES_DIR / f"{name}.json"
    if user.exists():
        return user
    prefixed_user = USER_STRATEGIES_DIR / f"strategy_{name}.json"
    if prefixed_user.exists():
        return prefixed_user
    builtin = APP_DIR / f"strategy_{name}.json"
    if builtin.exists():
        return builtin
    return None


def list_strategies() -> list[dict]:
    """Return all known strategies (built-in + user)."""
    seen: dict[str, dict] = {}
    for p in sorted(APP_DIR.glob("strategy_*.json")):
        name = p.stem.removeprefix("strategy_")
        seen[name] = {"name": name, "source": "builtin"}
    if USER_STRATEGIES_DIR.exists():
        for p in sorted(USER_STRATEGIES_DIR.glob("*.json")):
            name = p.stem.removeprefix("strategy_")
            if STRATEGY_NAME_RE.match(name):
                seen[name] = {"name": name, "source": "user"}  # user overrides builtin
    return list(seen.values())


def read_strategy(name: str) -> dict | None:
    p = _strategy_path(name)
    if not p:
        return None
    try:
        with p.open() as f:
            content = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "name": name,
        "source": "user" if p.parent == USER_STRATEGIES_DIR else "builtin",
        "content": content,
    }


def write_user_strategy(name: str, content: dict) -> tuple[bool, str]:
    if not STRATEGY_NAME_RE.match(name):
        return False, "invalid name (use letters, digits, _ or -; 1-40 chars)"
    USER_STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_STRATEGIES_DIR / f"{name}.json"
    try:
        path.write_text(json.dumps(content, indent=2))
    except OSError as e:
        return False, f"write failed: {e}"
    return True, str(path)


def seed_bot_dir(bot_id: str, strategy: str) -> None:
    """Create /var/lib/lp-ranger/bots/<id>/ and drop the chosen strategy.json.

    Idempotent: overwrites strategy.json but leaves any existing state alone.
    Looks up the strategy in both built-in and user locations.
    """
    bot_dir = BOTS_DIR / bot_id
    (bot_dir / ".local" / "share" / "lp-ranger").mkdir(parents=True, exist_ok=True)
    src = _strategy_path(strategy)
    if src is None:
        raise FileNotFoundError(f"strategy {strategy!r} not found in builtin or user dirs")
    try:
        with src.open() as f:
            content = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"invalid strategy source {src}: {e}") from e
    if not isinstance(content, dict):
        raise RuntimeError(f"invalid strategy source {src}: top-level JSON must be an object")
    content["selected_name"] = strategy
    (bot_dir / "strategy.json").write_text(json.dumps(content, indent=2))
    _bot_strategy_name_file(bot_id).write_text(strategy)


# --- Handler ---

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 — base class signature
        pass

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text_download(self, filename: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_auth_required(self) -> None:
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="LP Ranger Pi"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authenticated(self) -> bool:
        password = _auth_password()
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        import base64
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
        except Exception:
            return False
        _, _, supplied = decoded.partition(":")
        return hmac.compare_digest(supplied, password)

    def _csrf_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        for header_name in ("Origin", "Referer"):
            value = self.headers.get(header_name)
            if not value:
                continue
            parsed = urllib.parse.urlparse(value)
            if parsed.netloc.lower() != host:
                return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):  # noqa: N802
        if not self._is_authenticated():
            self._send_auth_required()
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                self._send_html(INDEX_FILE.read_bytes())
            except FileNotFoundError:
                self._send_json(500, {"ok": False, "error": "index.html missing"})
            return

        if path == "/api/health":
            bots = discover_bots()
            self._send_json(200, {
                "ok": True,
                "uptime_seconds": int(time.time() - _start_time),
                "bots_count": len(bots),
                "hostname": socket.gethostname(),
                "strategies": [s["name"] for s in list_strategies()],
                "auth_enabled": auth_enabled(),
            })
            return

        if path == "/api/bots":
            self._send_json(200, discover_bots())
            return

        if path.startswith("/api/state/"):
            bot_id = unquote(path[len("/api/state/"):])
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            if not (BOTS_DIR / bot_id).is_dir():
                self._send_json(404, {"ok": False, "error": "bot not found"})
                return
            self._send_json(200, read_state(bot_id))
            return

        if path.startswith("/api/bots/") and path.endswith("/config"):
            bot_id = unquote(path[len("/api/bots/"):-len("/config")])
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            self._send_json(200, {"ok": True, "config": read_bot_config(bot_id)})
            return

        if path.startswith("/api/bots/") and path.endswith("/download-log"):
            bot_id = unquote(path[len("/api/bots/"):-len("/download-log")])
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            data_dir = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger"
            log_file = data_dir / "daemon.log"
            try:
                body = log_file.read_bytes()
            except FileNotFoundError:
                body = b""
            except OSError as e:
                self._send_json(500, {"ok": False, "error": f"log read failed: {e}"})
                return
            self._send_text_download(f"lp-ranger-bot-{bot_id}.log", body)
            return

        if path.startswith("/api/bots/") and path.endswith("/download-diagnostics"):
            bot_id = unquote(path[len("/api/bots/"):-len("/download-diagnostics")])
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            if not (BOTS_DIR / bot_id).is_dir():
                self._send_json(404, {"ok": False, "error": "bot not found"})
                return
            body = diagnostics_text(bot_id)
            self._send_text_download(f"lp-ranger-bot-{bot_id}-diagnostics.txt", body)
            return

        if path == "/api/update/status":
            try:
                self._send_json(200, git_status())
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/strategies":
            self._send_json(200, list_strategies())
            return

        if path.startswith("/api/strategies/"):
            name = unquote(path[len("/api/strategies/"):])
            s = read_strategy(name)
            if s is None:
                self._send_json(404, {"ok": False, "error": "strategy not found"})
                return
            self._send_json(200, s)
            return

        if path == "/api/positions":
            qs = urllib.parse.parse_qs(parsed.query)
            wallet = (qs.get("wallet", [""])[0] or "").strip()
            if not _is_valid_wallet(wallet):
                self._send_json(400, {"ok": False, "error": "invalid wallet"})
                return
            try:
                positions = positions_for_wallet(wallet)
            except Exception as e:  # noqa: BLE001
                self._send_json(502, {"ok": False, "error": f"rpc: {e}"})
                return
            self._send_json(200, {"ok": True, "wallet": wallet, "positions": positions})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._is_authenticated():
            self._send_auth_required()
            return
        if not self._csrf_ok():
            self._send_json(403, {"ok": False, "error": "cross-site request blocked"})
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Upsert a user strategy. POST (create) and PUT (update) go through
        # the same handler here since the payload + validation are identical.
        if path.startswith("/api/strategies/"):
            name = unquote(path[len("/api/strategies/"):])
            if not STRATEGY_NAME_RE.match(name):
                self._send_json(400, {"ok": False, "error": "invalid name"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0 or length > MAX_STRATEGY_BYTES:
                self._send_json(413 if length else 400, {"ok": False, "error": "missing or oversize body"})
                return
            try:
                raw = self.rfile.read(length).decode()
                content = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_json(400, {"ok": False, "error": f"invalid JSON: {e}"})
                return
            if not isinstance(content, dict):
                self._send_json(400, {"ok": False, "error": "strategy JSON must be an object"})
                return
            ok, msg = write_user_strategy(name, content)
            if ok:
                self._send_json(200, {"ok": True, "name": name, "path": msg})
            else:
                self._send_json(400, {"ok": False, "error": msg})
            return

        if path == "/api/update/check":
            try:
                self._send_json(200, git_check_updates())
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/update/install":
            try:
                status = git_install()
                # Which bots were running at install time — client asks user to restart them.
                status["affected_bots"] = discover_bots()
                self._send_json(200, status)
                _restart_web_service_async()
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path.startswith("/api/bots/") and path.endswith("/restart"):
            bot_id = path[len("/api/bots/"):-len("/restart")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            ok, msg = _systemctl("restart", bot_id)
            if ok:
                self._send_json(200, {"ok": True, "message": msg or "restarted"})
            else:
                self._send_json(500, {"ok": False, "error": msg or "systemctl failed"})
            return

        if path.startswith("/api/bots/") and path.endswith("/ack-error"):
            bot_id = path[len("/api/bots/"):-len("/ack-error")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            data_dir = BOTS_DIR / bot_id / ".local" / "share" / "lp-ranger"
            if not data_dir.is_dir():
                self._send_json(404, {"ok": False, "error": "bot not found"})
                return
            latest_error = latest_error_info(tail_log(data_dir / "daemon.log"))
            if latest_error is None:
                self._send_json(200, {"ok": True, "message": "no pending errors", "has_unseen_error": False})
                return
            try:
                write_error_ack(data_dir, latest_error)
            except OSError as e:
                self._send_json(500, {"ok": False, "error": f"ack write failed: {e}"})
                return
            self._send_json(200, {"ok": True, "message": "error acknowledged", "has_unseen_error": False})
            return

        if path.startswith("/api/bots/") and path.endswith("/clear-log"):
            bot_id = path[len("/api/bots/"):-len("/clear-log")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            ok, msg = clear_bot_logs(bot_id)
            if ok:
                self._send_json(200, {"ok": True, "message": msg})
            else:
                self._send_json(500, {"ok": False, "error": msg})
            return

        if path.startswith("/api/bots/") and path.endswith("/start"):
            bot_id = path[len("/api/bots/"):-len("/start")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            body = self._read_json()
            strategy = body.get("strategy", "exit_pool")
            valid_names = {s["name"] for s in list_strategies()}
            if strategy not in valid_names:
                self._send_json(400, {"ok": False, "error": f"unknown strategy {strategy!r}"})
                return
            state = _read_bot_state_file(bot_id)
            try:
                close_strategy_performance_session(bot_id, state)
                seed_bot_dir(bot_id, strategy)
                update_strategy_performance(bot_id, state, strategy)
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": f"seed: {e}"})
                return
            ok, msg = _systemctl("enable-now", bot_id)
            if ok:
                self._send_json(200, {"ok": True, "message": msg or "started"})
            else:
                self._send_json(500, {"ok": False, "error": msg or "systemctl failed"})
            return

        if path.startswith("/api/bots/") and path.endswith("/strategy"):
            bot_id = path[len("/api/bots/"):-len("/strategy")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            body = self._read_json()
            strategy = body.get("strategy", "exit_pool")
            valid_names = {s["name"] for s in list_strategies()}
            if strategy not in valid_names:
                self._send_json(400, {"ok": False, "error": f"unknown strategy {strategy!r}"})
                return
            state = _read_bot_state_file(bot_id)
            previous_strategy = bot_strategy_name(bot_id)
            try:
                if previous_strategy:
                    update_strategy_performance(bot_id, state, previous_strategy)
                seed_bot_dir(bot_id, strategy)
                update_strategy_performance(bot_id, state, strategy)
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": f"seed: {e}"})
                return
            ok, msg = _systemctl("restart", bot_id)
            if ok:
                self._send_json(200, {"ok": True, "message": msg or "strategy updated"})
            else:
                self._send_json(500, {"ok": False, "error": msg or "systemctl failed"})
            return

        if path.startswith("/api/bots/") and path.endswith("/stop"):
            bot_id = path[len("/api/bots/"):-len("/stop")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            state = _read_bot_state_file(bot_id)
            close_strategy_performance_session(bot_id, state)
            ok, msg = _systemctl("disable-now", bot_id)
            if ok:
                self._send_json(200, {"ok": True, "message": msg or "stopped"})
            else:
                self._send_json(500, {"ok": False, "error": msg or "systemctl failed"})
            return

        if path.startswith("/api/bots/") and path.endswith("/config"):
            bot_id = path[len("/api/bots/"):-len("/config")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            body = self._read_json()
            # Validate target_usd: null clears it, positive number sets it
            target_usd = body.get("target_usd")
            if target_usd is not None and not isinstance(target_usd, (int, float)):
                self._send_json(400, {"ok": False, "error": "target_usd must be a number or null"})
                return
            if isinstance(target_usd, (int, float)) and target_usd <= 0:
                self._send_json(400, {"ok": False, "error": "target_usd must be positive"})
                return
            cfg = read_bot_config(bot_id)
            if target_usd is None:
                cfg.pop("target_usd", None)
            else:
                cfg["target_usd"] = float(target_usd)
            ok, msg = write_bot_config(bot_id, cfg)
            if ok:
                self._send_json(200, {"ok": True, "config": cfg})
            else:
                self._send_json(500, {"ok": False, "error": msg})
            return

        if path.startswith("/api/bots/") and path.endswith("/deploy-capital"):
            bot_id = path[len("/api/bots/"):-len("/deploy-capital")]
            if not bot_id.isdigit():
                self._send_json(400, {"ok": False, "error": "invalid bot id"})
                return
            cfg = read_bot_config(bot_id)
            cfg["request_recapitalize"] = True
            ok, msg = write_bot_config(bot_id, cfg)
            if ok:
                self._send_json(200, {"ok": True, "message": "deploy requested — daemon will execute in the next cycle"})
            else:
                self._send_json(500, {"ok": False, "error": msg})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    # Treat PUT identically to POST for strategies (some clients prefer PUT for upserts).
    do_PUT = do_POST  # noqa: N815


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"lp-web listening on {HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
