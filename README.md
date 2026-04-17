# LP Ranger

Automated liquidity pool manager for **WETH/USDC on Uniswap V3 (Base chain)**.

Desktop app with system tray indicator, embedded terminal, and autonomous on-chain execution.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![GTK](https://img.shields.io/badge/GTK-3.0-green)
![Chain](https://img.shields.io/badge/Chain-Base-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

## What it does

- **Monitors** your Uniswap V3 position in real-time (price, range, fees, IL)
- **Detects** when you're out of range or when a strong trend suggests exiting
- **Executes** rebalances, pool exits, and re-entries automatically by signing transactions
- **Shows** a traffic light in your Ubuntu system tray: 🟢 in range, 🟡 watch, 🔴 action needed

## Screenshots

The app has 3 tabs:

- **Dashboard** — Semaphore, metrics, and action recommendations
- **Config** — Position ID, range, strategy selector, history
- **Terminal** — Live log + command input for manual operations

## Quick start

```bash
# Clone
git clone https://github.com/YOUR_USER/lp-ranger.git
cd lp-ranger

# Install
chmod +x install.sh
./install.sh

# Install web3 for on-chain execution
pip install web3 --break-system-packages

# Set up wallet (first time only)
lp-autobot --setup

# Run
lp-ranger -p YOUR_POSITION_ID
```

## How it works

1. Checks ETH price every 5 min (CoinGecko / Binance, free)
2. Reads your position on-chain every 15 min (Base RPC, free)
3. Evaluates strategy: EMAs, RSI, ATR, distance to range edges
4. If signal detected + password unlocked → auto-executes via smart contracts
5. After rebalance, reads new position ID from chain and updates automatically

## Strategies

| Strategy | APR (backtested) | Rebalances/year | Style |
|----------|-----------------|-----------------|-------|
| `strategy_exit_pool.json` | **164.6%** | 12 | Exits pool in strong trends, holds ETH or USDC |
| `strategy_v1.json` | 52.2% | 11 | Trend-following with 18% range |
| `strategy_aggressive.json` | 55.6% | 48 | Fixed narrow 10% range |

Strategies are JSON files. Swap them anytime from the app without touching code.

## Modules

| File | Purpose |
|------|---------|
| `lp_ranger.py` | Main GTK app (dashboard + config + terminal) |
| `lp_autobot.py` | On-chain transaction executor |
| `lp_bridge.py` | Bridge to Claude Code for AI-reviewed execution |
| `lp_daemon.py` | Headless 24/7 daemon for dedicated PC |
| `install.sh` | Installer for Ubuntu |
| `setup_dedicated_pc.sh` | Full setup script for a dedicated machine |

## Terminal commands

Type these in the embedded terminal tab:

```
status                                    — Wallet balances + position
rebalance --price-lower X --price-upper Y — Change range
rebalance ... --dry-run                   — Simulate without executing
exit --hold ETH                           — Close pool, convert to ETH
exit --hold USDC                          — Close pool, convert to USDC
enter --price-lower X --price-upper Y     — Open new pool
reset-fees                                — Reset fee/IL counters
unlock                                    — Re-enter wallet password
forget                                    — Clear password from memory
help                                      — Show all commands
```

## Contracts (Base chain)

| Contract | Address |
|----------|---------|
| NonfungiblePositionManager | `0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1` |
| SwapRouter02 | `0x2626664c2603336E57B271c5C0b26F421741e481` |
| WETH | `0x4200000000000000000000000000000000000006` |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |

## Security

- Private key encrypted with AES-256 (password-protected)
- Password only held in memory, cleared on app close
- 4-hour cooldown between automatic actions
- Gas balance checked before every transaction
- `--dry-run` mode for testing
- Dedicated `lpranger` user (on dedicated PC setup)

## Updating

```bash
cd ~/lp-ranger
git pull
./install.sh
```

Your data (config, stats, history, encrypted key) is stored in `~/.local/share/lp-ranger/` and is never overwritten.

## Requirements

- Ubuntu 22.04+ (or any Linux with GTK 3)
- Python 3.10+
- `python3-gi`, `gir1.2-ayatanaappindicator3-0.1`
- `web3` (pip, for on-chain execution)

## License

MIT
