# LP Ranger — Context for Claude Code

## Overview
Autonomous WETH/USDC liquidity pool manager for Uniswap V3 on Base chain. Detects signals, executes rebalances/exits/entries by signing on-chain transactions automatically.

## Architecture
- `lp_ranger.py` — GTK app: 3 tabs (Dashboard, Config, Terminal), system tray indicator, auto-execution
- `lp_autobot.py` — On-chain executor: signs tx with encrypted private key, supports `--yes` for unattended mode
- `lp_bridge.py` — Optional bridge to Claude Code for AI-reviewed execution
- `lp_daemon.py` — Headless 24/7 daemon for dedicated PC

## Auto-execution flow
1. App starts → asks password via GTK dialog (hidden with ●) → caches in memory
2. Every 5 min: fetch price → evaluate strategy → if signal + password cached → execute automatically
3. Execution: calls `lp_autobot.py --rebalance/-exit/-enter --yes` with password via stdin
4. After execution: parses new position ID from output, updates config, re-fetches on-chain data

## Key contracts (Base, chain ID 8453)
- NonfungiblePositionManager: `0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1`
- SwapRouter02: `0x2626664c2603336E57B271c5C0b26F421741e481`
- WETH: `0x4200000000000000000000000000000000000006` (18 decimals)
- USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (6 decimals)
- Tick to price: `price_usdc_per_eth = 1.0001^tick × 10^12`

## Strategies (JSON files)
- `strategy_exit_pool.json` — **164.6% APR**: exits pool on strong trends (EMA divergence >10%), holds ETH or USDC, re-enters when lateral. Parameters: base_width 15%, trend_shift 0.4, buffer 5%, exit_trend 10%, enter_trend 2%.
- `strategy_v1.json` — 52.2% APR: trend-following with 18% range
- `strategy_aggressive.json` — 55.6% APR: fixed narrow 10% range

## Semaphore states
- `green` → in range, stable → no action
- `yellow` → near edge or out but within buffer → no action
- `red` → out of range + buffer exceeded → AUTO-REBALANCE
- `exit_eth` / `exit_usdc` → strong trend → AUTO-EXIT pool
- `closed_eth` / `closed_usdc` → pool closed, waiting → AUTO-ENTER when lateral

## Technical indicators
- EMA 20/50: trend direction. Divergence >10% = strong trend = exit signal
- ATR 14: volatility measurement
- RSI 14: >75 overbought, <25 oversold, used for reversal detection

## Data files (~/.local/share/lp-ranger/)
- `config.json` — position_id, range_lo/hi, strategy_file path
- `stats.json` — total_fees, fees_today, total_il, il_segments[], pool_active, hold_asset
- `history.json` — event log (max 500 entries)
- `.keystore.enc` — AES-256 encrypted private key (NEVER commit this)

## Safety
- Encrypted key (AES-256 with user password)
- 4h cooldown between actions
- Gas balance check before every tx
- Nonce tracking to prevent tx collisions
- 10 min timeout for blockchain operations
- `--dry-run` flag for testing
- `--yes` flag skips confirmation prompts (used by auto-execution)

## Common issues
- **Timeout**: Base chain can be slow. 10 min timeout should be enough. If not, check RPC endpoint.
- **Nonce collision**: `_reset_nonce()` called before each operation sequence.
- **Gas**: need ~0.0003 ETH for a full rebalance (3 txs). Check with `check_gas()`.
- **New position ID**: rebalance creates a new NFT. App parses `tokenId: XXXXX` from autobot output.
- **Fee estimation**: ~$0.31/day at 15.63% range width, scaled inversely. Only counted once per 5 min.

## Terminal commands
`status`, `rebalance --price-lower X --price-upper Y [--dry-run]`, `exit --hold ETH|USDC`, `enter --price-lower X --price-upper Y`, `reset-fees`, `unlock`, `forget`, `help`, `clear`

## Updating
```bash
git pull
./install.sh
# Data in ~/.local/share/lp-ranger/ is preserved
```
