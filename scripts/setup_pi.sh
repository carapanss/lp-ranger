#!/usr/bin/env bash
# ============================================================
# LP Ranger — Raspberry Pi installer (Pi Zero 2 W, Pi 3, Pi 4)
#
# Sets up the headless daemon on Raspberry Pi OS Lite 64-bit:
#   - installs python + web3 deps (uses aarch64 wheels, no compilation)
#   - copies the repo into /opt/lp-ranger
#   - migrates / creates the encrypted keystore
#   - writes /etc/systemd/system/lp-ranger.service from the template
#   - enables + starts the unit
#
# Assumes you have already SSH'd into the Pi and cloned this repo.
# Run from the repo root:
#   sudo bash scripts/setup_pi.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/lp-ranger"
PI_USER="${SUDO_USER:-$(id -un)}"
PI_HOME="$(getent passwd "$PI_USER" | cut -d: -f6)"
PASSWORD_FILE="$PI_HOME/.lp-password"
DATA_DIR="$PI_HOME/.local/share/lp-ranger"
SERVICE_SRC="$(cd "$(dirname "$0")" && pwd)/lp-ranger.service"
SERVICE_DST="/etc/systemd/system/lp-ranger.service"
UPDATE_SVC_SRC="$(cd "$(dirname "$0")" && pwd)/lp-ranger-update.service"
UPDATE_SVC_DST="/etc/systemd/system/lp-ranger-update.service"
UPDATE_TIMER_SRC="$(cd "$(dirname "$0")" && pwd)/lp-ranger-update.timer"
UPDATE_TIMER_DST="/etc/systemd/system/lp-ranger-update.timer"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log()  { printf "\033[1;34m[lp-ranger]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[lp-ranger]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[lp-ranger]\033[0m %s\n" "$*" >&2; exit 1; }

# ---- sanity ----------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  die "Run with sudo: sudo bash scripts/setup_pi.sh"
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  warn "Detected arch '$ARCH' — expected aarch64 (Raspberry Pi OS 64-bit)."
  warn "web3.py has no 32-bit ARM wheels; if this is 32-bit you'll have to"
  warn "compile from source. Reflash with Raspberry Pi OS Lite 64-bit."
  read -r -p "Continue anyway? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

if [[ ! -f "$REPO_DIR/lp_daemon.py" ]]; then
  die "Cannot find lp_daemon.py in $REPO_DIR. Run this script from the repo."
fi

# ---- packages --------------------------------------------------------------
log "Installing apt packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git ca-certificates

log "Installing Python dependencies (web3, cryptography)..."
# --break-system-packages is required on RPi OS Bookworm (PEP 668).
# Installing as root into the system Python is fine here — this box does one
# thing. If you prefer a venv, edit the ExecStart in lp-ranger.service.
pip3 install --break-system-packages --upgrade pip >/dev/null
pip3 install --break-system-packages web3 cryptography requests

# ---- install files ---------------------------------------------------------
log "Copying repo to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='tests' \
  --exclude='backtest/data' \
  --exclude='backtest/reports' \
  "$REPO_DIR"/ "$INSTALL_DIR"/
chown -R "$PI_USER:$PI_USER" "$INSTALL_DIR"

# ---- data dir --------------------------------------------------------------
log "Ensuring data dir $DATA_DIR"
sudo -u "$PI_USER" mkdir -p "$DATA_DIR"

# ---- keystore --------------------------------------------------------------
KEYSTORE="$DATA_DIR/.keystore.enc"
if [[ ! -f "$KEYSTORE" ]]; then
  warn "No keystore found at $KEYSTORE."
  warn "Either:"
  warn "  (a) scp your existing keystore from the laptop:"
  warn "      scp ~/.local/share/lp-ranger/.keystore.enc $PI_USER@<pi-ip>:$KEYSTORE"
  warn "  (b) run the setup interactively now:"
  warn "      sudo -u $PI_USER python3 $INSTALL_DIR/lp_autobot.py --setup"
  read -r -p "Run --setup now? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    sudo -u "$PI_USER" python3 "$INSTALL_DIR/lp_autobot.py" --setup
  else
    warn "Skipping keystore setup. systemd unit will be installed but the"
    warn "daemon won't start until a keystore exists."
  fi
fi

# ---- password file ---------------------------------------------------------
if [[ ! -f "$PASSWORD_FILE" ]]; then
  warn "No password file at $PASSWORD_FILE."
  warn "The daemon needs this to unlock the keystore unattended on boot."
  read -r -s -p "Enter keystore password (stored mode 600 at $PASSWORD_FILE): " pw
  echo
  if [[ -n "$pw" ]]; then
    printf '%s' "$pw" > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    chown "$PI_USER:$PI_USER" "$PASSWORD_FILE"
    log "Password file written."
  else
    warn "Empty password — skipping. Create the file manually before starting the service."
  fi
fi

# ---- systemd unit ----------------------------------------------------------
log "Installing systemd unit to $SERVICE_DST"
sed \
  -e "s|__USER__|$PI_USER|g" \
  -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  -e "s|__PASSWORD_FILE__|$PASSWORD_FILE|g" \
  -e "s|__DATA_DIR__|$DATA_DIR|g" \
  "$SERVICE_SRC" > "$SERVICE_DST"
chmod 644 "$SERVICE_DST"

log "Installing auto-update timer"
install -m 644 "$UPDATE_SVC_SRC"   "$UPDATE_SVC_DST"
install -m 644 "$UPDATE_TIMER_SRC" "$UPDATE_TIMER_DST"

log "Installing lp-ack helper"
install -m 755 "$(dirname "$SERVICE_SRC")/lp-ack" /usr/local/bin/lp-ack

systemctl daemon-reload
systemctl enable lp-ranger.service >/dev/null
systemctl enable --now lp-ranger-update.timer >/dev/null

if [[ -f "$KEYSTORE" && -f "$PASSWORD_FILE" ]]; then
  log "Starting lp-ranger.service..."
  systemctl restart lp-ranger.service
  sleep 2
  systemctl --no-pager --full status lp-ranger.service || true
else
  warn "Not starting the service — keystore or password file is missing."
  warn "Once both exist, start with: sudo systemctl start lp-ranger"
fi

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LP Ranger installed on $(hostname)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Install dir : $INSTALL_DIR
  Data dir    : $DATA_DIR
  Keystore    : $KEYSTORE
  Password    : $PASSWORD_FILE (mode 600)
  Service     : lp-ranger.service (enabled)

  Useful commands:
    sudo systemctl status lp-ranger        # service state
    sudo systemctl restart lp-ranger       # restart after config change
    sudo journalctl -u lp-ranger -f        # live systemd log
    tail -f $DATA_DIR/daemon.log           # app log
    tail -f $DATA_DIR/errors.log           # errors-only log
    lp-ack                                 # clear error flag, stop LED blink
    lp-ack --tail                          # also dump last 30 error lines
    systemctl list-timers lp-ranger-update # next auto-update check

  Auto-discovery is ON by default: when you rebalance from the laptop,
  the new NFT is picked up within POSITION_SYNC_SECONDS (5 min).

  Pulling an update from the laptop:
    cd $REPO_DIR && git pull && sudo bash scripts/setup_pi.sh

EOF
