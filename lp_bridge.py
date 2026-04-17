#!/usr/bin/env python3
"""
LP Ranger Bridge — Connects LP Ranger signals to Claude Code for review & execution.

Architecture:
  LP Ranger (GTK app) → writes proposal.json → Bridge (this script) → Claude Code → lp_autobot.py

The bridge:
1. Watches for new proposals from LP Ranger
2. Gathers context (price, position, strategy, history)  
3. Sends everything to Claude Code for review
4. Claude Code decides whether to execute and runs lp_autobot if yes

USAGE:
  # Run as daemon (watches for proposals continuously)
  python3 lp_bridge.py --daemon

  # Process a single proposal
  python3 lp_bridge.py --process

  # Test: generate a sample proposal and send to Claude
  python3 lp_bridge.py --test
"""

import json
import os
import sys
import time
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

DATA_DIR = Path.home() / ".local" / "share" / "lp-ranger"
PROPOSAL_FILE = DATA_DIR / "pending_proposal.json"
PROPOSAL_HISTORY = DATA_DIR / "proposal_history.json"
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
HISTORY_FILE = DATA_DIR / "history.json"
APP_DIR = Path(__file__).parent.resolve()
AUTOBOT = APP_DIR / "lp_autobot.py"

# Check interval when running as daemon
CHECK_INTERVAL_SECONDS = 300  # 5 minutes

# Hours during which the bot should NOT operate (your sleep time, Spain)
QUIET_HOURS_START = 23  # 11 PM
QUIET_HOURS_END = 8     # 8 AM


def load_json(path):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return {}


def is_quiet_hours():
    """Disabled — crypto markets are 24/7, bot should run always."""
    return False


def gather_context():
    """Gather all context needed for Claude to make a decision."""
    config = load_json(CONFIG_FILE)
    stats = load_json(STATS_FILE)
    history = load_json(HISTORY_FILE) if HISTORY_FILE.exists() else []
    
    # Load current strategy
    strategy_file = config.get("strategy_file", "")
    strategy = {}
    if strategy_file and Path(strategy_file).exists():
        strategy = load_json(Path(strategy_file))
    
    # Get recent history (last 20 events)
    recent = history[-20:] if isinstance(history, list) else []
    
    return {
        "position_id": config.get("position_id", ""),
        "current_range_lo": config.get("range_lo", 0),
        "current_range_hi": config.get("range_hi", 0),
        "strategy": strategy,
        "stats": {
            "total_fees_earned": stats.get("total_fees", 0),
            "total_il": stats.get("total_il", 0),
            "pool_active": stats.get("pool_active", True),
            "hold_asset": stats.get("hold_asset"),
            "il_segments": stats.get("il_segments", [])[-10:],
            "range_changes": stats.get("range_changes", [])[-10:],
        },
        "recent_history": recent,
    }


def build_claude_prompt(proposal, context):
    """Build a detailed prompt for Claude Code to review the proposal."""
    
    action_type = proposal.get("type", "unknown")
    
    prompt = f"""Eres el revisor de una estrategia de liquidity pool WETH/USDC en Uniswap V3 (Base chain).
LP Ranger ha detectado una señal y propone la siguiente acción. Tu trabajo es:
1. Revisar si la acción es óptima dados los datos
2. Si es buena, ejecutar el comando de autobot
3. Si no es óptima, rechazarla

=== PROPUESTA ===
Tipo: {action_type}
Fecha: {proposal.get("timestamp", "?")}
Precio ETH actual: ${proposal.get("current_price", 0):,.2f}
{f'Rango propuesto: ${proposal.get("proposed_lo", 0):,.0f} — ${proposal.get("proposed_hi", 0):,.0f} ({proposal.get("proposed_width", 0):.0f}%)' if action_type in ("rebalance", "enter_pool") else ""}
{f'Hold asset: {proposal.get("hold_asset", "?")}' if action_type == "exit_pool" else ""}
Motivo: {proposal.get("reason", "sin motivo")}

=== POSICIÓN ACTUAL ===
Position ID: {context.get("position_id", "?")}
Rango actual: ${context.get("current_range_lo", 0):,.0f} — ${context.get("current_range_hi", 0):,.0f}
Pool activa: {context["stats"].get("pool_active", True)}
{f'Holding: {context["stats"].get("hold_asset", "?")}' if not context["stats"].get("pool_active") else ""}

=== ESTADÍSTICAS ACUMULADAS ===
Fees totales ganadas: ${context["stats"].get("total_fees_earned", 0):.2f}
IL total acumulado: -${context["stats"].get("total_il", 0):.2f}
Cambios de rango recientes: {len(context["stats"].get("range_changes", []))}

=== ESTRATEGIA ACTIVA ===
Nombre: {context.get("strategy", {}).get("name", "?")}
Tipo: {context.get("strategy", {}).get("strategy_type", "?")}
Parámetros: {json.dumps(context.get("strategy", {}).get("parameters", {}), indent=2)}
Backtest APR: {context.get("strategy", {}).get("backtest_results", {}).get("net_apr", "?")}%

=== HISTORIAL RECIENTE ===
{chr(10).join([f"  {e.get('ts','')} | {e.get('type','')} | {e.get('msg','')}" for e in context.get("recent_history", [])[-10:]])}

=== INDICADORES DE LA SEÑAL ===
EMA 20: ${proposal.get("ema_fast", 0):,.2f}
EMA 50: ${proposal.get("ema_slow", 0):,.2f}
Tendencia: {proposal.get("trend", "?")} ({proposal.get("trend_pct", 0):+.1f}%)
RSI: {proposal.get("rsi", 50):.0f}
Volatilidad: {proposal.get("volatility_pct", 0):.2f}%

=== TU DECISIÓN ===
Analiza si esta acción es coherente con la estrategia y si el timing es bueno.
Cosas a considerar:
- ¿El impermanent loss del cambio se recuperará con las fees futuras?
- ¿La tendencia justifica el tipo de acción (rebalanceo vs salir)?
- ¿No estamos haciendo demasiados cambios (cada cambio cuesta ~2% en slippage)?
- ¿El rango propuesto es razonable para la volatilidad actual?
- ¿Estamos en horario activo (no noche en España)?

Si decides EJECUTAR, ejecuta el comando correspondiente:
"""

    pid = context.get("position_id", "")
    
    if action_type == "rebalance":
        prompt += f"""
```bash
python3 {AUTOBOT} --rebalance -p {pid} --price-lower {proposal.get("proposed_lo", 0):.0f} --price-upper {proposal.get("proposed_hi", 0):.0f}
```
Responde 'y' cuando pida confirmación.
"""
    elif action_type == "exit_pool":
        hold = proposal.get("hold_asset", "USDC")
        prompt += f"""
```bash
python3 {AUTOBOT} --exit -p {pid} --hold {hold}
```
Responde 'y' cuando pida confirmación.
"""
    elif action_type == "enter_pool":
        prompt += f"""
```bash
python3 {AUTOBOT} --enter --price-lower {proposal.get("proposed_lo", 0):.0f} --price-upper {proposal.get("proposed_hi", 0):.0f}
```
Responde 'y' cuando pida confirmación.
"""

    prompt += f"""
Si decides NO ejecutar, solo explica por qué y no ejecutes ningún comando.

Después de tu decisión, guarda el resultado ejecutando:
```bash
echo '{{"decision": "EXECUTED" o "REJECTED", "reason": "tu explicación"}}' > {DATA_DIR}/last_decision.json
```
"""
    
    return prompt


def send_to_claude(prompt):
    """Send the prompt to Claude Code for review and execution."""
    
    # Check if claude CLI is available
    claude_path = shutil.which("claude")
    if not claude_path:
        print("[Bridge] ERROR: 'claude' CLI not found. Install Claude Code first.")
        print("         See: https://docs.anthropic.com/en/docs/claude-code")
        return False
    
    print(f"[Bridge] Sending proposal to Claude Code for review...")
    print(f"[Bridge] Claude path: {claude_path}")
    
    try:
        # Run claude with the prompt
        # Using --print for non-interactive mode, or pipe to interactive
        result = subprocess.run(
            [claude_path, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout
            cwd=str(APP_DIR),
        )
        
        print(f"[Bridge] Claude response:")
        print(result.stdout)
        if result.stderr:
            print(f"[Bridge] Stderr: {result.stderr}")
        
        return result.returncode == 0
        
    except subprocess.TimeoutExpired:
        print("[Bridge] ERROR: Claude Code timed out (5 min)")
        return False
    except Exception as e:
        print(f"[Bridge] ERROR: {e}")
        return False


def process_proposal():
    """Check for pending proposal and process it."""
    
    if not PROPOSAL_FILE.exists():
        return False
    
    proposal = load_json(PROPOSAL_FILE)
    if not proposal:
        return False
    
    print(f"\n{'='*60}")
    print(f"[Bridge] New proposal detected!")
    print(f"  Type: {proposal.get('type', '?')}")
    print(f"  Time: {proposal.get('timestamp', '?')}")
    print(f"  Price: ${proposal.get('current_price', 0):,.2f}")
    print(f"  Reason: {proposal.get('reason', '?')}")
    print(f"{'='*60}")
    
    # Check quiet hours
    if is_quiet_hours():
        print(f"[Bridge] Quiet hours ({QUIET_HOURS_START}:00-{QUIET_HOURS_END}:00). Deferring.")
        return False
    
    # Gather context
    context = gather_context()
    
    # Build prompt
    prompt = build_claude_prompt(proposal, context)
    
    # Send to Claude
    success = send_to_claude(prompt)
    
    # Archive proposal regardless of outcome
    archive_proposal(proposal, success)
    
    # Remove pending proposal
    PROPOSAL_FILE.unlink(missing_ok=True)
    
    return success


def archive_proposal(proposal, was_processed):
    """Save proposal to history."""
    history = []
    if PROPOSAL_HISTORY.exists():
        try:
            with open(PROPOSAL_HISTORY) as f:
                history = json.load(f)
        except:
            pass
    
    proposal["processed"] = was_processed
    proposal["processed_at"] = datetime.now().isoformat()[:19]
    history.append(proposal)
    history = history[-200:]  # Keep last 200
    
    with open(PROPOSAL_HISTORY, "w") as f:
        json.dump(history, f, indent=2)


def daemon():
    """Run as a daemon, checking for proposals periodically."""
    print(f"[Bridge] Daemon started. Checking every {CHECK_INTERVAL_SECONDS}s")
    print(f"[Bridge] Quiet hours: {QUIET_HOURS_START}:00 - {QUIET_HOURS_END}:00 (Spain)")
    print(f"[Bridge] Watching: {PROPOSAL_FILE}")
    print(f"[Bridge] Press Ctrl+C to stop\n")
    
    while True:
        try:
            if PROPOSAL_FILE.exists():
                process_proposal()
            time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\n[Bridge] Daemon stopped.")
            break
        except Exception as e:
            print(f"[Bridge] Error: {e}")
            time.sleep(60)


def test():
    """Generate a test proposal and process it."""
    print("[Bridge] Generating test proposal...")
    
    test_proposal = {
        "type": "rebalance",
        "timestamp": datetime.now().isoformat()[:19],
        "current_price": 2216.38,
        "proposed_lo": 2007,
        "proposed_hi": 2404,
        "proposed_width": 18,
        "reason": "Fuera de rango (buffer 5% superado). Tendencia bajista.",
        "ema_fast": 2200.50,
        "ema_slow": 2250.30,
        "trend": "bajista",
        "trend_pct": -2.2,
        "rsi": 45,
        "volatility_pct": 1.8,
    }
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROPOSAL_FILE, "w") as f:
        json.dump(test_proposal, f, indent=2)
    
    print(f"[Bridge] Test proposal written to {PROPOSAL_FILE}")
    print(f"[Bridge] Processing...")
    process_proposal()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LP Ranger Bridge — Claude Code integration")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (continuous)")
    parser.add_argument("--process", action="store_true", help="Process pending proposal once")
    parser.add_argument("--test", action="store_true", help="Generate test proposal and process")
    parser.add_argument("--quiet-start", type=int, default=23, help="Quiet hours start (default: 23)")
    parser.add_argument("--quiet-end", type=int, default=8, help="Quiet hours end (default: 8)")
    args = parser.parse_args()
    
    QUIET_HOURS_START = args.quiet_start
    QUIET_HOURS_END = args.quiet_end
    
    if args.test:
        test()
    elif args.process:
        process_proposal()
    elif args.daemon:
        daemon()
    else:
        parser.print_help()
