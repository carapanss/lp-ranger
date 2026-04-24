# LP Ranger — Context for Claude Code

## Documentation discipline
- `CLAUDE.md` is the canonical handoff file for this project. Any meaningful architectural, operational, deployment, or workflow change must be reflected here in the same change set.
- Agent behavior preference: when a user request suggests adjacent improvements, proactively propose small, relevant follow-up features instead of waiting to be asked. Keep those suggestions concrete and scoped.
- If a future chat/agent adds or modifies components outside this repo (for example a Raspberry Pi web UI, a helper service, a second repo, a cron/timer, or a systemd unit override), document:
  - where that code lives
  - how it is deployed
  - which process serves/runs it
  - which files are the source of truth
  - any cross-host coordination assumptions
- Do not assume undocumented components are discoverable later. If something is running only on the Pi, record the path/repo/service name here before closing the task.
- Raspberry Pi web UI discovery (`2026-04-23`):
  - Service: `lp-web.service`
  - Listener: Python process on `0.0.0.0:8080`
  - Installed path on the Pi: `/opt/lp-ranger/web`
  - Files confirmed on the Pi: `/opt/lp-ranger/web/index.html`, `/opt/lp-ranger/web/lp_web.py`
  - Recovery status: the web files were copied back into the laptop repo under `web/` on `2026-04-23` so they can be versioned from now on.
  - The Pi also does not have a clone at `~/lp-ranger`; the live install is under `/opt/lp-ranger`.
  - The per-position daemon on the Pi is a systemd template unit: `/etc/systemd/system/lp-daemon@.service`.
  - Source-of-truth template for that unit now lives in the repo at `scripts/lp-daemon@.service`.
  - Source-of-truth template for the Pi web service now lives in the repo at `scripts/lp-web.service`.
  - Operator constraint: interactions with the Pi must be driven from the laptop terminal over SSH; do not assume direct shell access on the Pi outside that path.
  - Product constraint: keep the Pi web UI lightweight and operationally focused. It runs alongside the daemon on constrained hardware, so prefer low-RSS, stdlib-first features that help monitoring/control without competing with the bot for RAM.
  - Resource intent: the Raspberry Pi deployment target is a Pi Zero 2 W class box (512 MB RAM). The bot must keep comfortable headroom. Avoid adding heavy web stacks, background workers, or browser-side complexity unless there is a strong operational reason.
  - Feature split: the desktop app can own richer/manual controls because it runs on the laptop. The Pi web UI should focus on remote monitoring, log inspection, lightweight controls, updates, and diagnostics rather than duplicating every desktop action.
  - Security hardening (`2026-04-23`): the Pi web dashboard now supports lightweight HTTP Basic Auth driven by `/var/lib/lp-ranger/web/password`. If that file is absent or empty, auth is effectively off and the UI should be treated as LAN-exposed admin tooling.
  - Web POST endpoints now perform a basic same-origin check (`Origin`/`Referer` vs `Host`) to reduce casual CSRF on authenticated sessions.
  - The web update flow now schedules a self-restart of `lp-web.service` after a successful install so dashboard code changes actually go live.
  - `lp-web.service` must not set `NoNewPrivileges=true`, because the dashboard intentionally relies on narrowly-scoped sudoers rules for bot control, bot restarts, and self-update flows.
  - Strategy selection hardening in the Pi web UI (`2026-04-23`): the dashboard must persist the canonical strategy id for each bot (`exit_pool`, `optimal`, etc.) instead of inferring it from human-readable JSON fields like `name` or generic fields like `strategy_type`. Otherwise the selector can appear to revert to `exit_pool` after refresh even when the bot's `strategy.json` was updated.
  - Daemon startup hardening (`2026-04-23`): the Pi live bot should use the explicit `--shared-wallet-live` mode instead of relying on the old implicit `--dry-run` auto-promotion path.
  - Strategy consistency hardening (`2026-04-23`): the desktop app should route strategy decisions through `lp_core.evaluate_strategy` and only translate the result for UI presentation. Avoid re-implementing signal logic in GTK-only code.
  - Backtest IL hardening (`2026-04-24`): `backtest/engine.py` no longer ranks strategies with the old V2-style LP factor approximation. It now values positions from explicit V3 liquidity + token composition, and tracks realised IL per segment from the delta versus the held entry inventory. Treat any report generated before this date as stale for APR / IL comparisons.
  - Experimental optimal search (`2026-04-24`): `backtest/experiment_optimal.py` is a targeted search harness for two ideas that are not well covered by the generic grid search: adaptive-width fixed ranges and exit-to-idle-yield overlays. Current exploratory winner is stored separately in `strategy_optimal_1.json`; do not overwrite `strategy_optimal.json` until a broader walk-forward confirms it.
  - Web APR tracking (`2026-04-24`): the Pi dashboard now exposes a lightweight APR/PnL panel per bot plus a wallet-level summary for the visible tracked bots. This is intentionally computed from existing bot state (`daemon_state.json`) and the already-fetched NFT position data, rather than adding a resident worker or extra RPC-heavy backend path.
  - Strategy APR tracking (`2026-04-24`): the Pi dashboard now persists per-bot strategy performance to `/var/lib/lp-ranger/bots/<id>/.local/share/lp-ranger/strategy_performance.json`. The tracker closes and reopens sessions on bot start/stop and on strategy changes, accumulates real deltas of `fees` and `IL` while that strategy was active, and records detected pool `cash in/out` events.
  - TWR accounting (`2026-04-24`): strategy APR in the Pi dashboard is now annualised from a time-weighted return chain, not from simple `PnL / capital`. The tracker treats pool liquidity changes on the same NFT as external cash flows by repricing the previous and current liquidity at the same current ETH price, so wallet transfers outside the pool do not count as bot profit and pool top-ups / partial closes are excluded from return. This is the preferred metric for comparing strategies.
  - Daemon state enrichment (`2026-04-24`): `lp_daemon.py` now persists `tracking_started_at`, `position_liquidity`, `position_tick_lower`, and `position_tick_upper` into `daemon_state.json` so the web UI can estimate live pool capital and maintain APR tracking without extra RPC calls.
  - Backtest search hardening (`2026-04-23`): walk-forward search should fail closed when too many candidate configs error, instead of silently selecting a winner from a badly degraded search window.
  - Desktop install path on the laptop is `~/.local/share/lp-ranger/app`. That installed copy can drift from the repo checkout if `install.sh` is not rerun or files are not manually synced.
  - Practical mismatch seen on `2026-04-23`: laptop config was using `strategy_optimal.json` while the Pi bot for position `5009590` was using `Exit Pool Strategy v1`. When desktop and Pi disagree, verify both the strategy file and the installed app copy before blaming the bot.
  - Hardening and monitoring batch (`2026-04-24`):
    - `lp_daemon.py`: `State._load()` bare `except` replaced by specific `json.JSONDecodeError`/`OSError` with WARN logs. `can_act()` now uses `datetime.utcnow()` for daily reset to avoid local-timezone drift on Pi. `price_history` now has a hard cap of 500 entries in addition to the 60-day TTL. `fetch_price()` outer `except` narrowed to `except Exception`. Bot ID validation (`bot_id.isdigit()`) was already present on all POST endpoints in `lp_web.py`.
    - RAM monitoring: `lp_daemon.py` now reads `/proc/self/status` (VmRSS) each cycle and writes `ram_rss_kb` + a 48-entry circular `ram_history` (4 h at 5 min) into `daemon_state.json`. Also writes `last_cycle_at` timestamp for the web badge.
    - Pi web UI RAM panel: `web/index.html` now shows current RSS plus a scaled mini-chart from `ram_history` in each running-bot card, with axis labels and `warn`/`crit` reference lines. Thresholds remain >200 MB = amber, >400 MB = red.
    - Pi web UI "last seen" badge: the running/stale tag in each card now includes the cycle age (`Xs ago` / `Xm ago`) so you can tell at a glance if the daemon is responding.
    - Log collapsing in the console modal: consecutive "routine ok" lines (🟢/🟡, no errors/actions/strategy changes) are collapsed to `first … N identical ok lines … last`, keeping all signal and error lines visible. The full log is still downloadable unchanged.
  - Capital control per bot (`2026-04-24`): the daemon now reads `/var/lib/lp-ranger/bots/<id>/.local/share/lp-ranger/bot_config.json` before each rebalance/enter and, if `target_usd` is set, passes `--target-usd` to the autobot. The Pi web UI exposes a "capital" button in each running-bot card (prompt → POST `/api/bots/{id}/config`). Setting it to empty restores "all available" behavior. This prevents the daemon from deploying more capital than intended when the wallet has leftover from prior runs.

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
- `strategy_optimal.json` — **261.6% walk-forward OOS APR** (6/6 positive 30d windows, median 259.5%, max DD 0.2%): fixed 5% width, buffer 0. Winner of a ~7k-config walk-forward grid search (180d train / 30d test, step 30d) over the last 365 d of ETHUSDT hourly. Maximizes fee capture at the narrowest feasible band; the frequent rebalances (~1/day) are paid back by the inversely-scaled fee rate ($0.31 × 15.63/width_pct). See `backtest/reports/`.
- `strategy_exit_pool.json` — ~90% APR on real 365d data (documented 164% was unverified): exits pool on strong trends (EMA divergence >10%), holds ETH or USDC, re-enters when lateral. Parameters: base_width 15%, trend_shift 0.4, buffer 5%, exit_trend 10%, enter_trend 2%.
- `strategy_v1.json` — trend-following with 18% range (unvalidated claim 52%)
- `strategy_aggressive.json` — fixed narrow 10% range (unvalidated claim 55%)

## Backtesting
- `backtest/engine.py` — deterministic hourly-candle replay; shares `lp_core.evaluate_strategy` with the live daemon so ranking matches production behavior. Precomputed indicator streams make each 180d backtest ~5 ms.
- `backtest/search.py --walk-forward` — grid search + rolling validation. Ranks by mean OOS APR to punish overfit.
- `backtest/data_loader.py` — Binance ETHUSDT 1h loader, cached to `backtest/data/`.

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

## Raspberry Pi deployment (headless 24/7)
`lp_daemon.py` runs standalone on a Pi Zero 2 W (or any aarch64 SBC) with no GUI. Set up:
```bash
# On the Pi (Raspberry Pi OS Lite 64-bit, SSH enabled):
git clone <repo-url> lp-ranger && cd lp-ranger
sudo bash scripts/setup_pi.sh
```
The installer copies the repo to `/opt/lp-ranger`, installs `web3`/`cryptography` (aarch64 wheels, no compilation), prompts for the keystore + unlock password, writes them to `~/.lp-password` mode 600, and enables `lp-ranger.service` (systemd — restarts on crash, MemoryMax=200M). Keystore copied from the laptop with `scp ~/.local/share/lp-ranger/.keystore.enc pi@<ip>:~/.local/share/lp-ranger/` works too.

### Tutorial paso a paso (Pi Zero 2 W, primera instalación)

Asume: Pi Zero 2 W + microSD ≥16 GB + cargador oficial + cable Ethernet (vía el hub USB-OTG). Tiempo total: ~20 min.

**1. Flashear la microSD (en el portátil)**

- Descarga **Raspberry Pi Imager**: <https://www.raspberrypi.com/software/>
- Inserta la microSD en el portátil (adaptador USB si hace falta).
- En Imager:
  - *Device*: Raspberry Pi Zero 2 W
  - *Operating System* → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**. ⚠️ El de 32-bit NO sirve: `web3` no tiene wheels precompiladas ahí.
  - *Storage*: la microSD.
- Antes de escribir, pulsa el engranaje (⚙️) para **personalizar**:
  - Hostname: `lpranger` (opcional, facilita el SSH)
  - Usuario + contraseña: crea un usuario (ej. `pi` / contraseña a tu gusto — esta SÍ la vas a necesitar, anótala)
  - WiFi: SSID + contraseña de tu red + país (ES)
  - Locale: zona horaria `Europe/Madrid`, teclado `es`
  - **Services** → marca **Enable SSH** → *Use password authentication*
- *Write*. Espera ~5 min. Al acabar, extrae la SD.

**2. Primer arranque del Pi**

- Mete la microSD en el Pi Zero 2 W.
- Conecta el hub USB+Ethernet al puerto micro-USB de datos (el marcado `USB`, no el `PWR`) y el cable Ethernet al router.
- Conecta el cargador al puerto `PWR`. El Pi arranca (LED verde).
- Espera ~2 min al primer boot (se expande la partición + conecta a red).

**3. Entrar por SSH desde el portátil**

- Encuentra la IP: mira la tabla de DHCP de tu router, o prueba:
  ```bash
  ping lpranger.local
  ```
- Conecta:
  ```bash
  ssh pi@lpranger.local      # o ssh pi@<IP>
  ```
  Primera vez: acepta la huella (`yes`). Escribe la contraseña del paso 1.

**4. Clonar el repo en el Pi**

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/carapanss/lp-ranger.git
cd lp-ranger
```

**5. Copiar el keystore desde el portátil (en otra terminal, en el portátil)**

```bash
scp ~/.local/share/lp-ranger/.keystore.enc pi@lpranger.local:/tmp/keystore.enc
```

**6. Correr el instalador (en el Pi)**

```bash
mkdir -p ~/.local/share/lp-ranger
mv /tmp/keystore.enc ~/.local/share/lp-ranger/.keystore.enc
sudo bash scripts/setup_pi.sh
```

El script:
- Instala `python3`, `web3`, `cryptography` (wheels aarch64, ~2 min).
- Copia el repo a `/opt/lp-ranger`.
- Te pide la contraseña del keystore → la guarda en `~/.lp-password` modo 600.
- Renderiza `/etc/systemd/system/lp-ranger.service` desde la plantilla.
- Activa y arranca el servicio.

**7. Verificar que funciona**

```bash
sudo systemctl status lp-ranger       # debe decir "active (running)"
sudo journalctl -u lp-ranger -f       # ver el log en vivo — Ctrl+C para salir
tail -f ~/.local/share/lp-ranger/daemon.log
```

En el log deberías ver:
```
[AutoBot] Keystore unlocked — wallet 0x...
Auto-discovered active position 12345 — adopting
🟢 ETH $2,350 | Range $2,200-$2,500 | green | trend +0.2% | RSI 55
```

### Operaciones comunes en el Pi

```bash
sudo systemctl restart lp-ranger              # tras editar una estrategia
sudo systemctl stop lp-ranger                 # parar sin deshabilitar
sudo systemctl start lp-ranger                # arrancar
sudo journalctl -u lp-ranger -n 100 --no-pager   # últimas 100 líneas
tail -f ~/.local/share/lp-ranger/daemon.log   # log completo (INFO+)
tail -f ~/.local/share/lp-ranger/errors.log   # solo WARN/ERROR (1 MB × 3)
lp-ack                                         # apagar LED tras un error
lp-ack --tail                                  # + dump últimas 30 líneas
systemctl list-timers lp-ranger-update         # próximo auto-update
```

### LED de error (Pi onboard)

El LED verde `ACT` del Pi pasa a parpadear (200 ms on / 200 ms off) cuando el daemon emite cualquier log de nivel ERROR. El disparador es un archivo flag en `~/.local/share/lp-ranger/error.flag` que acumula una línea por error. Cuando lo ves parpadear desde casa, te conectas por SSH, corres `lp-ack` y el LED vuelve a su trigger por defecto (mmc0 activity). `scripts/setup_pi.sh` instala `lp-ack` en `/usr/local/bin/`. Sin hardware extra — desactivable con `--no-led` en el ExecStart.

### Auto-update diario

`scripts/lp-ranger-update.timer` corre `scripts/lp-ranger-update.sh` una vez al día (+ 10 min tras boot si se perdió el slot, + jitter aleatorio de hasta 1 h). El script hace `git fetch`, si hay commits nuevos hace `git pull --ff-only` (nunca merge/rebase automático — diverge ⇒ aborta), vuelve a correr `setup_pi.sh` para refrescar deps + el unit, y reinicia el servicio. Log en `~/.local/share/lp-ranger/update.log`. Si el pull falla (conflicto), queda escrito allí y se mandará a errors.log vía el próximo ciclo ERROR.

Para actualizar manualmente sin esperar al timer:
```bash
sudo systemctl start lp-ranger-update.service
```

### Troubleshooting Pi

- **`active (running)` pero no loggea nada en `daemon.log`:** el servicio está escribiendo a journald pero el directorio `~/.local/share/lp-ranger/` no es writable por el user del servicio. `ls -la ~/.local/share/lp-ranger` y fija permisos.
- **`FATAL: cannot unlock keystore`:** la contraseña en `~/.lp-password` no coincide con la que encriptó el keystore. `echo -n 'tu-password' > ~/.lp-password && chmod 600 ~/.lp-password && sudo systemctl restart lp-ranger`.
- **Falla `pip install web3` con compilación:** flasheaste la versión 32-bit por error. Vuelve al paso 1 con la 64-bit.
- **No veo el Pi en la red:** el hub USB-OTG suele necesitar alimentación externa para Ethernet estable. Si el LED del puerto Ethernet no se enciende, el cargador oficial (5V/2.5A) lo soluciona en el 99% de los casos.
- **RAM alta (>150 MB):** `sudo systemctl restart lp-ranger` una vez por semana vía cron si te preocupa. El daemon debería mantenerse en ~40-60 MB en régimen.

### Multi-host coordination (laptop + Pi, same wallet)
Both can run simultaneously against the same wallet. The blockchain is the source of truth — no inter-process protocol needed. When the laptop GUI rebalances (mints a new NFT) or adds capital, the daemon detects the change on its next poll via `lp_autobot.find_active_weth_usdc_position(address)` (ERC721Enumerable scan: `balanceOf` + `tokenOfOwnerByIndex` + `positions` filtered to WETH/USDC with `liquidity > 0`). Resync cadence = `POSITION_SYNC_SECONDS` (5 min). Strategy changes still require editing the strategy JSON and restarting the service on the Pi.

Disable with `--no-autodiscover` if you want to pin the daemon to a specific `--position-id`.
