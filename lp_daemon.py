#!/usr/bin/env python3
"""
LP Ranger Daemon — Headless 24/7 bot for VPS
No GUI needed. Runs strategy, sends to Claude Code for review, executes via AutoBot.

USAGE:
  # On your VPS:
  python3 lp_daemon.py --position-id 4950738 --password-file /root/.lp-password

  # With Claude Code review (recommended):
  python3 lp_daemon.py -p 4950738 --with-claude

  # Without Claude (auto-execute with safety limits):
  python3 lp_daemon.py -p 4950738 --auto-execute

  # Dry run (log decisions but don't execute):
  python3 lp_daemon.py -p 4950738 --dry-run

  # Raspberry Pi live mode via the shared unattended wallet:
  python3 lp_daemon.py -p 4950738 --shared-wallet-live
"""

import json, os, sys, math, time, subprocess, shutil, getpass, logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
import urllib.request
import lp_core

# ============================================================
# CONFIG
# ============================================================
APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path.home() / ".local" / "share" / "lp-ranger"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = DATA_DIR / "daemon.log"
ERROR_LOG_FILE = DATA_DIR / "errors.log"
ERROR_FLAG_FILE = DATA_DIR / "error.flag"
STATE_FILE = DATA_DIR / "daemon_state.json"
DEFAULT_PASSWORD_FILE = Path.home() / ".lp-password"
SHARED_WALLET_DIR = Path("/var/lib/lp-ranger/wallet")
SHARED_WALLET_KEYSTORE = SHARED_WALLET_DIR / ".local" / "share" / "lp-ranger" / ".keystore.enc"
SHARED_WALLET_PASSWORD = SHARED_WALLET_DIR / "password"

# How often to rescan the wallet for the current position NFT. The laptop GUI
# can mint a new NFT by rebalancing out-of-band, so the daemon has to pick it
# up instead of acting on the stale token id cached in state.
POSITION_SYNC_SECONDS = 300

# Pi onboard LED. Bookworm exposes the green ACT LED at /sys/class/leds/ACT.
# Some kernels expose it as 'led0' instead. We probe both.
LED_CANDIDATES = ["/sys/class/leds/ACT", "/sys/class/leds/led0"]

_DEFAULT_RPCS = [
    "https://mainnet.base.org",
    "https://base.publicnode.com",
    "https://base.llamarpc.com",
]
_env_rpcs = os.environ.get("LP_RPC_ENDPOINTS", "")
RPC_ENDPOINTS = [u.strip() for u in _env_rpcs.split(",") if u.strip()] or _DEFAULT_RPCS
BASE_RPC = RPC_ENDPOINTS[0]
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd&include_24hr_change=true"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDC"

POLL_SECONDS = 300        # Check every 5 min
COOLDOWN_SECONDS = 14400  # 4 hours between actions
MAX_ACTIONS_PER_DAY = 3   # Safety: max 3 rebalances per day

# ============================================================
# PI LED INDICATOR
# ============================================================
# Background thread that blinks the onboard ACT LED (heartbeat pattern,
# 200 ms on / 200 ms off) whenever ERROR_FLAG_FILE exists. The flag is
# set automatically by the ERROR-level log handler. The user clears it
# over SSH with `lp-ack` (scripts/lp-ack), which deletes the flag and
# restores the LED's default trigger.
import threading

def _find_led_dir():
    for p in LED_CANDIDATES:
        if Path(p).exists() and (Path(p) / "brightness").exists():
            return Path(p)
    return None

def _led_write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except (OSError, PermissionError):
        return False


def _read_rss_kb() -> int:
    """Read current process RSS from /proc/self/status. Returns 0 on any failure."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0

class _LedBlinker(threading.Thread):
    """Blinks the Pi ACT LED while ERROR_FLAG_FILE is present.

    Saves and restores the original trigger so non-error operation looks
    identical to a fresh Pi (mmc0 activity flicker).
    """
    def __init__(self):
        super().__init__(daemon=True, name="lp-led")
        self.led = _find_led_dir()
        self._orig_trigger = None
        self._stop = threading.Event()

    def _read_trigger(self):
        try:
            with open(self.led / "trigger") as f:
                # File looks like: "[mmc0] none heartbeat ..."; bracketed = active.
                tokens = f.read().split()
            for tok in tokens:
                if tok.startswith("[") and tok.endswith("]"):
                    return tok[1:-1]
        except OSError:
            pass
        return "mmc0"  # safe default on Pi

    def _enter_blink_mode(self):
        _led_write(self.led / "trigger", "none")
        _led_write(self.led / "brightness", "0")

    def _restore(self):
        _led_write(self.led / "trigger", self._orig_trigger or "mmc0")

    def run(self):
        if self.led is None:
            return  # not on a Pi (or LED not exposed) — no-op silently
        self._orig_trigger = self._read_trigger()
        blinking = False
        try:
            while not self._stop.is_set():
                if ERROR_FLAG_FILE.exists():
                    if not blinking:
                        self._enter_blink_mode()
                        blinking = True
                    _led_write(self.led / "brightness", "1")
                    time.sleep(0.2)
                    _led_write(self.led / "brightness", "0")
                    time.sleep(0.2)
                else:
                    if blinking:
                        self._restore()
                        blinking = False
                    time.sleep(2)
        finally:
            if blinking:
                self._restore()

    def stop(self):
        self._stop.set()


def _raise_error_flag(msg):
    """Set the LED-blink flag and append a one-line entry with timestamp."""
    try:
        ERROR_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_FLAG_FILE, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")
    except OSError:
        pass

# ============================================================
# LOGGING
# ============================================================
class _ErrorFlagHandler(logging.Handler):
    """logging handler — every ERROR triggers the LED-blink flag."""
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            _raise_error_flag(record.getMessage())

_LOGGER = logging.getLogger("lp_daemon")
if not _LOGGER.handlers:
    _LOGGER.setLevel(logging.INFO)
    _fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5)
    _fh.setFormatter(_fmt)
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    # Dedicated errors-only log so SSHing in for triage doesn't require
    # grep'ing through 25 MB of INFO chatter.
    _eh = RotatingFileHandler(ERROR_LOG_FILE, maxBytes=1*1024*1024, backupCount=3)
    _eh.setLevel(logging.WARNING)
    _eh.setFormatter(_fmt)
    _LOGGER.addHandler(_fh)
    _LOGGER.addHandler(_ch)
    _LOGGER.addHandler(_eh)
    _LOGGER.addHandler(_ErrorFlagHandler())

_LEVEL_MAP = {"INFO": logging.INFO, "WARN": logging.WARNING,
              "WARNING": logging.WARNING, "ERROR": logging.ERROR,
              "DEBUG": logging.DEBUG}

def log(msg, level="INFO"):
    _LOGGER.log(_LEVEL_MAP.get(level, logging.INFO), msg)

# ============================================================
# STATE MANAGEMENT
# ============================================================
class State:
    def __init__(self):
        self.data = {
            "position_id": "",
            "range_lo": 0, "range_hi": 0,
            "pool_active": True, "hold_asset": None,
            "tracking_started_at": 0,
            "position_liquidity": 0,
            "position_tick_lower": 0,
            "position_tick_upper": 0,
            "last_action_time": 0,
            "last_action_by_type": {},  # {action_type: unix_ts}
            "actions_today": 0, "actions_today_date": "",
            "total_fees": 0, "total_il": 0,
            "price_history": [],
        }
        self._load()
        self._ensure_tracking_metadata()

    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.data.update(json.load(f))
            except json.JSONDecodeError as e:
                log(f"State file corrupted, starting fresh: {e}", "WARN")
            except OSError as e:
                log(f"State file unreadable, starting fresh: {e}", "WARN")

    def _ensure_tracking_metadata(self):
        if self.data.get("tracking_started_at"):
            return
        try:
            started = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0
        except OSError:
            started = 0
        self.data["tracking_started_at"] = started or time.time()
        self.save()

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def get(self, k, d=None): return self.data.get(k, d)
    def set(self, k, v): self.data[k] = v; self.save()

    def can_act(self, action_type=None):
        """Check cooldown and daily limits. If action_type is supplied, apply
        a per-type cooldown; otherwise fall back to the global cooldown."""
        now = time.time()
        today = datetime.utcnow().strftime("%Y-%m-%d")  # UTC — Pi may lack NTP-synced localtime
        if self.data["actions_today_date"] != today:
            self.data["actions_today"] = 0
            self.data["actions_today_date"] = today
        if self.data["actions_today"] >= MAX_ACTIONS_PER_DAY:
            return False, f"Daily limit reached ({MAX_ACTIONS_PER_DAY})"
        by_type = self.data.get("last_action_by_type", {}) or {}
        last_same = by_type.get(action_type, 0) if action_type else 0
        last_global = self.data.get("last_action_time", 0)
        last = max(last_same, last_global)
        elapsed = now - last
        if elapsed < COOLDOWN_SECONDS:
            remaining = (COOLDOWN_SECONDS - elapsed) / 3600
            scope = f" for {action_type}" if action_type else ""
            return False, f"Cooldown{scope}: {remaining:.1f}h remaining"
        return True, "OK"

    def record_action(self, action_type=None):
        now = time.time()
        self.data["last_action_time"] = now
        if action_type:
            by_type = self.data.setdefault("last_action_by_type", {})
            by_type[action_type] = now
        self.data["actions_today"] += 1
        self.save()

    def add_price(self, price):
        self.data["price_history"].append({"t": time.time(), "p": price})
        cutoff = time.time() - 60 * 86400
        ph = [x for x in self.data["price_history"] if x["t"] > cutoff]
        if len(ph) > 500:  # hard cap regardless of age
            ph = ph[-500:]
        self.data["price_history"] = ph
        if len(ph) % 12 == 0:
            self.save()

# ============================================================
# PRICE & POSITION FETCHER
# ============================================================
def fetch_price():
    try:
        req = urllib.request.Request(COINGECKO_URL,
                headers={"Accept":"application/json","User-Agent":"LP-Daemon/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
            return d["ethereum"]["usd"], d["ethereum"].get("usd_24h_change", 0)
    except Exception:
        try:
            req = urllib.request.Request(BINANCE_URL, headers={"User-Agent":"LP-Daemon/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode())
                return float(d["lastPrice"]), float(d["priceChangePercent"])
        except Exception as e:
            log(f"Price fetch failed: {e}", "ERROR")
            return None, None

def fetch_position(pid):
    try:
        tid = hex(int(pid))[2:].zfill(64)
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_call",
            "params":[{"to":POSITION_MANAGER,"data":"0x99fbab88"+tid},"latest"],"id":1}).encode()
        result = None
        last_err = None
        for url in RPC_ENDPOINTS:
            try:
                req = urllib.request.Request(url, data=payload,
                        headers={"Content-Type":"application/json","User-Agent":"LP-Daemon/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
                break
            except Exception as e:
                last_err = e
                continue
        if result is None:
            raise RuntimeError(f"all RPCs failed: {last_err}")
        if "result" in result and len(result["result"]) > 10:
            raw = result["result"][2:]
            if len(raw) >= 64*12:
                fields = [raw[i*64:(i+1)*64] for i in range(12)]
                tl = int(fields[5],16); tu = int(fields[6],16)
                if tl >= 2**255: tl -= 2**256
                if tu >= 2**255: tu -= 2**256
                pl = 1.0001**tl * 1e12; pu = 1.0001**tu * 1e12
                if pl > pu: pl, pu = pu, pl
                return {"lo": round(pl,2), "hi": round(pu,2),
                        "fee": int(fields[4],16), "liq": int(fields[7],16),
                        "tl": tl, "tu": tu}
        return None
    except Exception as e:
        log(f"Position fetch failed: {e}", "ERROR")
        return None

# ============================================================
# STRATEGY ENGINE (embedded, no GTK dependency)
# ============================================================
_validate_strategy = lp_core.validate_strategy


class Strategy:
    DEFAULT_CFG = {"strategy_type":"exit_pool",
        "parameters":{"base_width_pct":15,"trend_shift":0.4,"buffer_pct":5,
                      "exit_trend_pct":10,"enter_trend_pct":2}}

    def __init__(self, path):
        self.cfg = {}
        p = Path(path)
        if not p.exists():
            log(f"Strategy not found: {path}, using defaults", "WARN")
            self.cfg = dict(self.DEFAULT_CFG); return
        try:
            with open(p) as f: cfg = json.load(f)
        except Exception as e:
            log(f"Strategy parse error in {path}: {e}. Using defaults.", "ERROR")
            self.cfg = dict(self.DEFAULT_CFG); return
        errs = _validate_strategy(cfg)
        if errs:
            log(f"Invalid strategy in {path}:", "ERROR")
            for e in errs: log(f"  - {e}", "ERROR")
            log("Falling back to defaults.", "WARN")
            self.cfg = dict(self.DEFAULT_CFG); return
        self.cfg = cfg
        log(f"Strategy loaded: {self.cfg.get('name','?')}")

    def evaluate(self, price, rlo, rhi, pool_active, hold_asset, price_history):
        state = {"range_lo": rlo, "range_hi": rhi,
                 "pool_active": pool_active, "hold_asset": hold_asset}
        return lp_core.evaluate_strategy(self.cfg, price, state, price_history)

    def execution_cfg(self):
        ex = self.cfg.get("execution", {})
        if not isinstance(ex, dict):
            ex = {}
        return {
            "cooldown_seconds": int(ex.get("cooldown_seconds", COOLDOWN_SECONDS)),
            "max_actions_per_day": int(ex.get("max_actions_per_day", MAX_ACTIONS_PER_DAY)),
        }

# ============================================================
# CLAUDE CODE INTEGRATION
# ============================================================
def review_with_claude(action, details, state):
    """Send proposal to Claude Code for review. Returns True if approved."""
    claude_path = shutil.which("claude")
    if not claude_path:
        log("Claude Code not found, skipping review", "WARN")
        return None  # None = no review available

    prompt = f"""Eres el revisor automático de un bot de liquidity pool WETH/USDC en Uniswap V3 (Base).

ACCIÓN PROPUESTA: {action['type']}
{f"Nuevo rango: ${action.get('lo',0):,.0f} — ${action.get('hi',0):,.0f}" if action['type'] != 'exit_pool' else f"Cerrar pool → hold {action.get('hold','USDC')}"}
Motivo: {action.get('reason','')}

DATOS:
- Precio ETH: ${details['price']:,.2f}
- Tendencia: {details['trend']} ({details['trend_pct']:+.1f}%)
- RSI: {details['rsi']:.0f}
- Volatilidad: {details['vol']:.2f}%
- Rango actual: ${state.get('range_lo',0):,.0f} — ${state.get('range_hi',0):,.0f}
- Pool activa: {state.get('pool_active')}
- Fees acumuladas: ${state.get('total_fees',0):.2f}
- IL acumulado: ${state.get('total_il',0):.2f}
- Acciones hoy: {state.get('actions_today',0)}/{MAX_ACTIONS_PER_DAY}

Responde SOLO con una línea:
APPROVE - si la acción es coherente y el timing es bueno
REJECT: [motivo] - si no es óptimo

Criterios: ¿El IL se recuperará? ¿La tendencia lo justifica? ¿No estamos sobretradeando?"""

    try:
        result = subprocess.run(
            [claude_path, "--print", prompt],
            capture_output=True, text=True, timeout=120
        )
        response = result.stdout.strip()
        log(f"Claude review: {response[:200]}")

        if "APPROVE" in response.upper():
            return True
        else:
            return False
    except Exception as e:
        log(f"Claude review error: {e}", "ERROR")
        return None

# ============================================================
# EXECUTOR
# ============================================================
def _read_bot_config() -> dict:
    """Read per-bot capital config from DATA_DIR/bot_config.json if present."""
    cfg_file = DATA_DIR / "bot_config.json"
    try:
        with open(cfg_file) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def execute_action(action, state, password=None, pk=None, dry_run=False, target_usd=None):
    """Execute an action via AutoBot.

    Prefers passing the already-decrypted private key via an anonymous pipe
    FD (``--pk-fd``) so the subprocess never sees the keystore password.
    Falls back to password-via-stdin only for backward compatibility.

    target_usd: if set and > 0, caps the position size to this USD amount.
    """
    autobot = str(APP_DIR / "lp_autobot.py")
    pid = str(state.get("position_id", ""))

    # Read per-bot capital config (written by the web UI) so the daemon
    # honours the target even if the caller didn't pass it explicitly.
    if target_usd is None or target_usd <= 0:
        cfg = _read_bot_config()
        t = cfg.get("target_usd")
        if isinstance(t, (int, float)) and t > 0:
            target_usd = float(t)

    if action["type"] == "rebalance":
        cmd = [sys.executable, autobot, "--rebalance", "-p", pid,
               "--price-lower", f"{action['lo']:.0f}",
               "--price-upper", f"{action['hi']:.0f}", "-y"]
        if target_usd and target_usd > 0:
            cmd += ["--target-usd", f"{target_usd:.2f}"]
    elif action["type"] == "exit_pool":
        cmd = [sys.executable, autobot, "--exit", "-p", pid,
               "--hold", action["hold"], "-y"]
    elif action["type"] == "enter_pool":
        cmd = [sys.executable, autobot, "--enter",
               "--price-lower", f"{action['lo']:.0f}",
               "--price-upper", f"{action['hi']:.0f}", "-y"]
        if target_usd and target_usd > 0:
            cmd += ["--target-usd", f"{target_usd:.2f}"]
    else:
        log(f"Unknown action type: {action['type']}", "ERROR")
        return False

    if dry_run:
        log(f"DRY RUN: Would execute: {' '.join(cmd)}")
        return True

    log(f"EXECUTING: {' '.join(cmd)}")
    run_kwargs = dict(capture_output=True, text=True, timeout=300, cwd=str(APP_DIR))
    try:
        if pk:
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, pk.encode())
            finally:
                os.close(w_fd)
            try:
                result = subprocess.run(
                    cmd + ["--pk-fd", str(r_fd)],
                    pass_fds=(r_fd,), **run_kwargs,
                )
            finally:
                try: os.close(r_fd)
                except OSError: pass
        else:
            result = subprocess.run(cmd, input=f"{password or ''}\n", **run_kwargs)

        log(f"AutoBot output: {result.stdout[-500:]}")
        if result.returncode == 0:
            log("Execution successful!")
            return True
        else:
            log(f"Execution failed: {result.stderr[-300:]}", "ERROR")
            return False
    except Exception as e:
        log(f"Execution error: {e}", "ERROR")
        return False

# ============================================================
# MAIN DAEMON LOOP
# ============================================================
def daemon_loop(args):
    global COOLDOWN_SECONDS, MAX_ACTIONS_PER_DAY
    state = State()
    strategy_path = args.strategy or str(APP_DIR / "strategy_exit_pool.json")
    strategy = Strategy(strategy_path)
    execution_cfg = strategy.execution_cfg()
    if args.cooldown is not None:
        COOLDOWN_SECONDS = args.cooldown
    else:
        COOLDOWN_SECONDS = execution_cfg["cooldown_seconds"]
    if args.max_actions_per_day is not None:
        MAX_ACTIONS_PER_DAY = args.max_actions_per_day
    else:
        MAX_ACTIONS_PER_DAY = execution_cfg["max_actions_per_day"]

    # Start the LED blinker (Pi only; no-op elsewhere). Background thread.
    led_thread = None
    if not args.no_led:
        led_thread = _LedBlinker()
        if led_thread.led is not None:
            led_thread.start()
            log(f"LED blinker armed on {led_thread.led}")
        else:
            log("No /sys/class/leds/ACT — LED indicator disabled (not on a Pi?)")

    if args.position_id:
        state.set("position_id", args.position_id)

    if args.shared_wallet_live:
        if not SHARED_WALLET_KEYSTORE.exists() or not SHARED_WALLET_PASSWORD.exists():
            missing = []
            if not SHARED_WALLET_KEYSTORE.exists():
                missing.append(str(SHARED_WALLET_KEYSTORE))
            if not SHARED_WALLET_PASSWORD.exists():
                missing.append(str(SHARED_WALLET_PASSWORD))
            log(
                "FATAL: shared-wallet live mode requested but missing: " + ", ".join(missing),
                "ERROR",
            )
            return
        args.dry_run = False
        if not args.password_file:
            args.password_file = str(SHARED_WALLET_PASSWORD)
        # Keep lp_autobot's home-based keystore lookup working for each bot.
        bot_keystore = Path.home() / ".local" / "share" / "lp-ranger" / ".keystore.enc"
        if not bot_keystore.exists():
            bot_keystore.parent.mkdir(parents=True, exist_ok=True)
            bot_keystore.symlink_to(SHARED_WALLET_KEYSTORE)
        log(f"Shared wallet live mode enabled via {SHARED_WALLET_DIR}")

    # Get password for autobot and derive the private key once, in-process.
    # After derivation we drop the password entirely so Scrypt never runs
    # again and the plaintext password is no longer in memory.
    password = None
    pk = None
    wallet_addr = None
    if not args.dry_run:
        pw_path = Path(args.password_file) if args.password_file else DEFAULT_PASSWORD_FILE
        if pw_path.exists():
            with open(pw_path) as f:
                password = f.read().strip()
            try:
                os.chmod(pw_path, 0o600)
            except OSError:
                pass
            log(f"Password loaded from {pw_path}")
        else:
            password = getpass.getpass("AutoBot unlock password: ")
        try:
            import lp_autobot
            pk = lp_autobot.decrypt_key(password)
            wallet_addr = lp_autobot.wallet_address_from_pk(pk)
            log(f"Keystore unlocked — wallet {wallet_addr}")
        except Exception as e:
            log(f"FATAL: cannot unlock keystore: {e}", "ERROR")
            return
        finally:
            # Drop the plaintext password — we will only use the derived PK.
            password = None

    # First-boot / laptop-rebalance auto-discovery: if we don't have a
    # position id yet, or if the wallet now holds a different NFT, adopt it.
    # Cheap two-eth_call probe, OK to run eagerly here.
    if wallet_addr and not args.no_autodiscover:
        try:
            import lp_autobot
            tid, pos = lp_autobot.find_active_weth_usdc_position(wallet_addr)
            if tid is not None:
                current = str(state.get("position_id") or "")
                if current != str(tid):
                    log(f"Auto-discovered active position {tid} (was {current or 'none'}) — adopting")
                    state.set("position_id", str(tid))
                    state.set("pool_active", True)
                    state.set("hold_asset", None)
            elif state.get("pool_active", True) and str(state.get("position_id") or ""):
                log("No active WETH/USDC NFT in wallet — marking pool as closed")
                state.set("pool_active", False)
        except Exception as e:
            log(f"Auto-discovery failed (non-fatal): {e}", "WARN")

    log("="*50)
    log(f"LP Ranger Daemon started")
    log(f"  Position: {state.get('position_id')}")
    log(f"  Strategy: {strategy.cfg.get('name','?')}")
    log(f"  Poll interval: {POLL_SECONDS}s")
    log(f"  Cooldown: {COOLDOWN_SECONDS/3600:.0f}h")
    log(f"  Max actions/day: {MAX_ACTIONS_PER_DAY}")
    log(f"  Mode: {'DRY RUN' if args.dry_run else 'Claude review' if args.with_claude else 'AUTO EXECUTE'}")
    log("="*50)

    while True:
        try:
            # 1. Fetch price
            price, change = fetch_price()
            if price is None:
                time.sleep(60); continue

            state.add_price(price)

            # 2a. Rescan the wallet for the *current* NFT. If the laptop GUI
            # minted a fresh position (rebalance → new tokenId) we adopt it
            # here so the daemon keeps operating on the live pool.
            if wallet_addr and not args.no_autodiscover:
                last_sync = state.data.get("last_position_sync_ts", 0)
                if (time.time() - last_sync) >= POSITION_SYNC_SECONDS:
                    try:
                        import lp_autobot
                        tid, _ = lp_autobot.find_active_weth_usdc_position(wallet_addr)
                        current = str(state.get("position_id") or "")
                        if tid is not None and current != str(tid):
                            log(f"Wallet now holds position {tid} (was {current or 'none'}) — resyncing")
                            state.set("position_id", str(tid))
                            state.set("pool_active", True)
                            state.set("hold_asset", None)
                            state.data["last_position_fetch_ts"] = 0  # force re-read below
                        elif tid is None and state.get("pool_active", True) and current:
                            log("Wallet no longer holds an active LP NFT — marking pool closed")
                            state.set("pool_active", False)
                    except Exception as e:
                        log(f"Position sync failed (non-fatal): {e}", "WARN")
                    state.data["last_position_sync_ts"] = time.time()
                    state.save()

            # 2b. Fetch position when wall-clock elapsed exceeds interval (default 15 min).
            pid = state.get("position_id")
            pos_interval = int(strategy.cfg.get("data_sources",{}).get("position_poll_interval_seconds", 900))
            last_pos_fetch = state.data.get("last_position_fetch_ts", 0)
            if pid and (time.time() - last_pos_fetch) >= pos_interval:
                pos = fetch_position(pid)
                if pos:
                    if state.get("range_lo") != pos["lo"] or state.get("range_hi") != pos["hi"]:
                        log(f"Position updated: ${pos['lo']:.0f}-${pos['hi']:.0f}")
                    state.set("range_lo", pos["lo"])
                    state.set("range_hi", pos["hi"])
                    state.data["position_liquidity"] = pos["liq"]
                    state.data["position_tick_lower"] = pos["tl"]
                    state.data["position_tick_upper"] = pos["tu"]
                    state.data["last_position_fetch_ts"] = time.time()
                    state.save()

            # 3. Evaluate strategy
            rlo = state.get("range_lo", 0)
            rhi = state.get("range_hi", 0)
            pool_active = state.get("pool_active", True)
            hold = state.get("hold_asset")

            status, action, det = strategy.evaluate(
                price, rlo, rhi, pool_active, hold, state.data["price_history"]
            )

            # Status line
            status_sym = {"green":"🟢","yellow":"🟡","red":"🔴","exit":"🚪","enter":"🔄","closed":"📦","gray":"⚪"}
            _rsi = det.get('rsi')
            _rsi_s = f"{_rsi:.0f}" if isinstance(_rsi,(int,float)) else "?"
            log(f"{status_sym.get(status,'?')} ETH ${price:,.0f} | Range ${rlo:,.0f}-${rhi:,.0f} | {status} | trend {det.get('trend_pct',0):+.1f}% | RSI {_rsi_s}")

            # 4. Act if there's a signal
            if action:
                can, reason = state.can_act(action_type=action.get("type"))
                if not can:
                    log(f"  Signal: {action['type']} but {reason}")
                    time.sleep(POLL_SECONDS); continue

                log(f"  🔔 SIGNAL: {action['type']} — {action.get('reason','')}")

                # Review with Claude if enabled
                approved = True
                if args.with_claude:
                    claude_result = review_with_claude(action, det, state.data)
                    if claude_result is True:
                        log("  ✅ Claude APPROVED")
                        approved = True
                    elif claude_result is False:
                        log("  ❌ Claude REJECTED")
                        approved = False
                        state.record_action(action_type=action.get("type"))  # Count as action to avoid spam
                    else:
                        log("  ⚠️ Claude unavailable, using auto-execute rules")
                        # Fallback: auto-approve if conservative checks pass
                        approved = True

                if approved:
                    success = execute_action(action, state, pk=pk, dry_run=args.dry_run)
                    if success:
                        state.record_action(action_type=action.get("type"))
                        # Update state based on action
                        if action["type"] == "exit_pool":
                            state.set("pool_active", False)
                            state.set("hold_asset", action.get("hold", "USDC"))
                            state.set("range_lo", 0); state.set("range_hi", 0)
                        elif action["type"] == "enter_pool":
                            state.set("pool_active", True)
                            state.set("hold_asset", None)
                            state.set("range_lo", action["lo"])
                            state.set("range_hi", action["hi"])
                        elif action["type"] == "rebalance":
                            state.set("range_lo", action["lo"])
                            state.set("range_hi", action["hi"])
                        log(f"  ✅ Action completed successfully")

            # Estimate fees if in range
            if pool_active and rlo > 0 and rhi > 0 and rlo <= price <= rhi:
                cw = (rhi-rlo)/((rlo+rhi)/2)*100
                fee_est = 0.31 * (15.63/max(cw,1)) / (24*12)
                state.data["total_fees"] = state.data.get("total_fees",0) + fee_est

            # RAM tracking — read RSS once per cycle, keep 48-entry circular buffer (4h at 5min)
            rss = _read_rss_kb()
            if rss:
                state.data["ram_rss_kb"] = rss
                ram_hist = state.data.setdefault("ram_history", [])
                ram_hist.append({"t": int(time.time()), "kb": rss})
                state.data["ram_history"] = ram_hist[-48:]
            state.data["last_cycle_at"] = time.time()
            state.save()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("Daemon stopped by user")
            state.save()
            break
        except Exception as e:
            log(f"Unhandled error: {e}", "ERROR")
            time.sleep(60)


# ============================================================
# VPS SETUP HELPER
# ============================================================
def setup_vps():
    """Print instructions for VPS deployment."""
    print("""
╔══════════════════════════════════════════════════════╗
║         LP Ranger — VPS Deployment Guide            ║
╚══════════════════════════════════════════════════════╝

1. RENT A VPS (~3-5€/month):
   • Hetzner Cloud: hetzner.com/cloud (CX22 = 3.29€/month)
   • DigitalOcean: 4$/month droplet
   • Contabo: contabo.com (VPS S = 4.99€/month)
   Choose Ubuntu 24.04, smallest plan is enough.

2. CONNECT & INSTALL:
   ssh root@YOUR_VPS_IP
   apt update && apt install -y python3 python3-pip
   pip3 install web3 --break-system-packages

   # Install Claude Code (for AI review):
   # See: https://docs.anthropic.com/en/docs/claude-code
   npm install -g @anthropic-ai/claude-code

3. UPLOAD LP RANGER:
   # From your PC:
   scp lp-ranger.tar.gz root@YOUR_VPS_IP:~/
   # On VPS:
   cd ~ && tar xzf lp-ranger.tar.gz && cd lp-ranger

4. SETUP WALLET KEY:
   python3 lp_autobot.py --setup
   # Save password to file for auto-unlock:
   echo "YOUR_PASSWORD" > ~/.lp-password
   chmod 600 ~/.lp-password

5. RUN AS SERVICE:
   # Create systemd service:
   cat > /etc/systemd/system/lp-ranger.service << EOF
[Unit]
Description=LP Ranger Daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/lp-ranger
ExecStart=/usr/bin/python3 /root/lp-ranger/lp_daemon.py -p YOUR_POSITION_ID --with-claude --password-file /root/.lp-password
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

   systemctl enable lp-ranger
   systemctl start lp-ranger

6. MONITOR:
   journalctl -u lp-ranger -f          # Live logs
   tail -f ~/.local/share/lp-ranger/daemon.log  # App logs

7. COSTS:
   • VPS: ~3-5€/month
   • Gas (Base): ~$0.01-0.05 per transaction
   • Claude Code API: depends on usage (~$0.03 per review)
   • Total: ~5-7€/month for 24/7 automated LP management
""")


if __name__ == "__main__":
    import argparse
    pa = argparse.ArgumentParser(description="LP Ranger Daemon — headless 24/7 bot")
    pa.add_argument("-p", "--position-id", help="Uniswap V3 position NFT ID")
    pa.add_argument("-s", "--strategy", help="Strategy JSON file")
    pa.add_argument("--with-claude", action="store_true", help="Use Claude Code for review")
    pa.add_argument("--auto-execute", action="store_true", help="Execute without Claude review")
    pa.add_argument("--dry-run", action="store_true", help="Log only, don't execute")
    pa.add_argument(
        "--shared-wallet-live",
        action="store_true",
        help="Run live using the Pi shared wallet in /var/lib/lp-ranger/wallet",
    )
    pa.add_argument("--password-file", help=f"File with AutoBot unlock password (default: {DEFAULT_PASSWORD_FILE})")
    pa.add_argument("--setup-vps", action="store_true", help="Show VPS setup instructions")
    pa.add_argument("--poll", type=int, default=300, help="Poll interval in seconds")
    pa.add_argument("--cooldown", type=int, help="Cooldown between actions in seconds")
    pa.add_argument("--max-actions-per-day", type=int, help="Maximum automated actions per day")
    pa.add_argument("--no-autodiscover", action="store_true",
                    help="Disable wallet-scan position auto-discovery (pin to --position-id)")
    pa.add_argument("--no-led", action="store_true",
                    help="Disable the Pi onboard LED error indicator")
    args = pa.parse_args()

    if args.setup_vps:
        setup_vps()
        sys.exit(0)

    POLL_SECONDS = args.poll
    daemon_loop(args)
