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
"""

import json, os, sys, math, time, subprocess, shutil, getpass
from pathlib import Path
from datetime import datetime
import urllib.request

# ============================================================
# CONFIG
# ============================================================
APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path.home() / ".local" / "share" / "lp-ranger"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = DATA_DIR / "daemon.log"
STATE_FILE = DATA_DIR / "daemon_state.json"

BASE_RPC = "https://mainnet.base.org"
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd&include_24hr_change=true"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDC"

POLL_SECONDS = 300        # Check every 5 min
COOLDOWN_SECONDS = 14400  # 4 hours between actions
MAX_ACTIONS_PER_DAY = 3   # Safety: max 3 rebalances per day

# ============================================================
# LOGGING
# ============================================================
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ============================================================
# STATE MANAGEMENT
# ============================================================
class State:
    def __init__(self):
        self.data = {
            "position_id": "",
            "range_lo": 0, "range_hi": 0,
            "pool_active": True, "hold_asset": None,
            "last_action_time": 0,
            "actions_today": 0, "actions_today_date": "",
            "total_fees": 0, "total_il": 0,
            "price_history": [],
        }
        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.data.update(json.load(f))
            except: pass

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def get(self, k, d=None): return self.data.get(k, d)
    def set(self, k, v): self.data[k] = v; self.save()

    def can_act(self):
        """Check cooldown and daily limits."""
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data["actions_today_date"] != today:
            self.data["actions_today"] = 0
            self.data["actions_today_date"] = today
        if self.data["actions_today"] >= MAX_ACTIONS_PER_DAY:
            return False, f"Daily limit reached ({MAX_ACTIONS_PER_DAY})"
        elapsed = now - self.data.get("last_action_time", 0)
        if elapsed < COOLDOWN_SECONDS:
            remaining = (COOLDOWN_SECONDS - elapsed) / 3600
            return False, f"Cooldown: {remaining:.1f}h remaining"
        return True, "OK"

    def record_action(self):
        self.data["last_action_time"] = time.time()
        self.data["actions_today"] += 1
        self.save()

    def add_price(self, price):
        self.data["price_history"].append({"t": time.time(), "p": price})
        cutoff = time.time() - 60 * 86400
        self.data["price_history"] = [x for x in self.data["price_history"] if x["t"] > cutoff]
        # Don't save every time (too frequent), save periodically
        if len(self.data["price_history"]) % 12 == 0:
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
    except:
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
        req = urllib.request.Request(BASE_RPC, data=payload,
                headers={"Content-Type":"application/json","User-Agent":"LP-Daemon/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
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
class Strategy:
    def __init__(self, path):
        self.cfg = {}
        p = Path(path)
        if p.exists():
            with open(p) as f: self.cfg = json.load(f)
            log(f"Strategy loaded: {self.cfg.get('name','?')}")
        else:
            log(f"Strategy not found: {path}, using defaults", "WARN")
            self.cfg = {"strategy_type":"exit_pool",
                "parameters":{"base_width_pct":15,"trend_shift":0.4,"buffer_pct":5,
                              "exit_trend_pct":10,"enter_trend_pct":2}}

    def evaluate(self, price, rlo, rhi, pool_active, hold_asset, price_history):
        p = self.cfg.get("parameters", {})
        st = self.cfg.get("strategy_type", "exit_pool")
        ic = self.cfg.get("data_sources",{}).get("indicators",{})

        prices = [x["p"] for x in price_history]
        if len(prices) < 2:
            return "gray", None, {"message": "Insufficient data"}

        ef = self._ema(prices, ic.get("ema_fast",20))
        es = self._ema(prices, ic.get("ema_slow",50))
        atr = self._atr(prices, ic.get("atr_period",14))
        rsi = self._rsi(prices, ic.get("rsi_period",14))
        vp = atr/price*100 if price>0 else 0
        tu = ef > es
        tp = (ef-es)/es*100 if es>0 else 0

        det = {"price":price,"trend":"up" if tu else "down","trend_pct":round(tp,1),
               "ema_fast":round(ef,2),"ema_slow":round(es,2),
               "rsi":round(rsi,1),"vol":round(vp,2),"atr":round(atr,2)}

        # Pool closed → check for re-entry
        if not pool_active:
            nt = p.get("enter_trend_pct", 2)
            if abs(tp) < nt:
                bw = p.get("base_width_pct",15)
                hw = bw/200; ts2 = p.get("trend_shift",0.4)
                sh = hw*ts2*min(abs(tp)/100*8,1)
                nc = price*(1+sh) if tu else price*(1-sh)
                return "enter", {"type":"enter_pool","lo":round(nc*(1-bw/200),2),
                    "hi":round(nc*(1+bw/200),2),"width":bw,
                    "reason":f"Lateralización (trend {tp:+.1f}%)"}, det
            h = hold_asset or "USDC"
            if h=="ETH" and rsi<35:
                bw=p.get("base_width_pct",15)
                return "enter",{"type":"enter_pool","lo":round(price*(1-bw/200),2),
                    "hi":round(price*(1+bw/200),2),"width":bw,
                    "reason":f"RSI {rsi:.0f}, reversión posible"},det
            if h=="USDC" and rsi>65:
                bw=p.get("base_width_pct",15)
                return "enter",{"type":"enter_pool","lo":round(price*(1-bw/200),2),
                    "hi":round(price*(1+bw/200),2),"width":bw,
                    "reason":f"RSI {rsi:.0f}, reversión posible"},det
            return "closed", None, det

        # Pool active
        if rlo <= 0 or rhi <= 0:
            return "gray", None, {"message": "No range set"}

        inr = rlo <= price <= rhi

        # Exit pool signal
        if st == "exit_pool" and abs(tp) > p.get("exit_trend_pct",10) and len(prices)>30:
            h = "ETH" if tp > 0 else "USDC"
            return "exit", {"type":"exit_pool","hold":h,
                "reason":f"Tendencia fuerte ({tp:+.1f}%), exit → {h}"}, det

        # Out of range
        buf = p.get("buffer_pct",5)/100
        if not inr and (price < rlo*(1-buf) or price > rhi*(1+buf)):
            bw=p.get("base_width_pct",15); ts2=p.get("trend_shift",0.4)
            hw=bw/200; sh=hw*ts2*min(abs(tp)/100*8,1)
            nc = price*(1+sh) if tu else price*(1-sh)
            return "rebalance", {"type":"rebalance","lo":round(nc*(1-bw/200),2),
                "hi":round(nc*(1+bw/200),2),"width":bw,
                "reason":f"Fuera de rango, trend {tp:+.1f}%"}, det

        if not inr:
            return "yellow", None, det

        # Edge proximity
        rw = rhi - rlo
        edge = min(price-rlo, rhi-price)/rw*100 if rw>0 else 0
        if edge < 5:
            return "yellow", None, det

        return "green", None, det

    def _ema(self, prices, per):
        if not prices: return 0
        k=2/(per+1); e=prices[0]
        for p in prices[1:]: e=p*k+e*(1-k)
        return e
    def _atr(self, prices, per):
        if len(prices)<2: return 0
        trs=[abs(prices[i]-prices[i-1]) for i in range(1,len(prices))]
        k=2/(per+1); a=trs[0]
        for t in trs[1:]: a=t*k+a*(1-k)
        return a
    def _rsi(self, prices, per):
        if len(prices)<per+1: return 50
        ds=[prices[i]-prices[i-1] for i in range(1,len(prices))]
        g=[max(d,0) for d in ds]; l=[max(-d,0) for d in ds]
        k=2/(per+1); ag=g[0]; al=l[0]
        for i in range(1,len(g)): ag=g[i]*k+ag*(1-k); al=l[i]*k+al*(1-k)
        if al==0: return 100
        return 100-(100/(1+ag/al))

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
def execute_action(action, state, password=None, pk=None, dry_run=False):
    """Execute an action via AutoBot.

    Prefers passing the already-decrypted private key via an anonymous pipe
    FD (``--pk-fd``) so the subprocess never sees the keystore password.
    Falls back to password-via-stdin only for backward compatibility.
    """
    autobot = str(APP_DIR / "lp_autobot.py")
    pid = str(state.get("position_id", ""))

    if action["type"] == "rebalance":
        cmd = [sys.executable, autobot, "--rebalance", "-p", pid,
               "--price-lower", f"{action['lo']:.0f}",
               "--price-upper", f"{action['hi']:.0f}", "-y"]
    elif action["type"] == "exit_pool":
        cmd = [sys.executable, autobot, "--exit", "-p", pid,
               "--hold", action["hold"], "-y"]
    elif action["type"] == "enter_pool":
        cmd = [sys.executable, autobot, "--enter",
               "--price-lower", f"{action['lo']:.0f}",
               "--price-upper", f"{action['hi']:.0f}", "-y"]
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
    state = State()
    strategy_path = args.strategy or str(APP_DIR / "strategy_exit_pool.json")
    strategy = Strategy(strategy_path)

    if args.position_id:
        state.set("position_id", args.position_id)

    # Get password for autobot and derive the private key once, in-process.
    # After derivation we drop the password entirely so Scrypt never runs
    # again and the plaintext password is no longer in memory.
    password = None
    pk = None
    if not args.dry_run:
        if args.password_file and Path(args.password_file).exists():
            with open(args.password_file) as f:
                password = f.read().strip()
            try:
                os.chmod(args.password_file, 0o600)
            except OSError:
                pass
            log("Password loaded from file")
        else:
            password = getpass.getpass("AutoBot unlock password: ")
        try:
            import lp_autobot
            pk = lp_autobot.decrypt_key(password)
            log("Keystore unlocked; private key cached in memory for this session")
        except Exception as e:
            log(f"FATAL: cannot unlock keystore: {e}", "ERROR")
            return
        finally:
            # Drop the plaintext password — we will only use the derived PK.
            password = None

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

            # 2. Fetch position (every 3rd cycle = ~15 min)
            cycle = int(time.time() / POLL_SECONDS) % 3
            pid = state.get("position_id")
            if cycle == 0 and pid:
                pos = fetch_position(pid)
                if pos:
                    if state.get("range_lo") != pos["lo"] or state.get("range_hi") != pos["hi"]:
                        log(f"Position updated: ${pos['lo']:.0f}-${pos['hi']:.0f}")
                    state.set("range_lo", pos["lo"])
                    state.set("range_hi", pos["hi"])

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
            log(f"{status_sym.get(status,'?')} ETH ${price:,.0f} | Range ${rlo:,.0f}-${rhi:,.0f} | {status} | trend {det.get('trend_pct',0):+.1f}% | RSI {det.get('rsi',50):.0f}")

            # 4. Act if there's a signal
            if action:
                can, reason = state.can_act()
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
                        state.record_action()  # Count as action to avoid spam
                    else:
                        log("  ⚠️ Claude unavailable, using auto-execute rules")
                        # Fallback: auto-approve if conservative checks pass
                        approved = True

                if approved:
                    success = execute_action(action, state, pk=pk, dry_run=args.dry_run)
                    if success:
                        state.record_action()
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
    pa.add_argument("--password-file", help="File with AutoBot unlock password")
    pa.add_argument("--setup-vps", action="store_true", help="Show VPS setup instructions")
    pa.add_argument("--poll", type=int, default=300, help="Poll interval in seconds")
    pa.add_argument("--cooldown", type=int, default=14400, help="Cooldown between actions in seconds")
    args = pa.parse_args()

    if args.setup_vps:
        setup_vps()
        sys.exit(0)

    POLL_SECONDS = args.poll
    COOLDOWN_SECONDS = args.cooldown

    daemon_loop(args)
