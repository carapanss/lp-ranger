#!/usr/bin/env bash
# Pull the latest code from the user's clone, reinstall, restart the
# daemon. Idempotent: if there are no new commits, do nothing.
#
# Layout assumption: scripts/setup_pi.sh installed to /opt/lp-ranger
# from a clone living at $REPO_DIR (default ~/lp-ranger of the service
# user). Override REPO_DIR / SERVICE / RUN_USER via env if needed.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/lp-ranger}"
SERVICE="${SERVICE:-lp-ranger.service}"
RUN_USER="${RUN_USER:-$(systemctl show -p User --value "$SERVICE" 2>/dev/null || echo pi)}"
REPO_DIR_DEFAULT="$(getent passwd "$RUN_USER" | cut -d: -f6)/lp-ranger"
REPO_DIR="${REPO_DIR:-$REPO_DIR_DEFAULT}"
LOG="$(getent passwd "$RUN_USER" | cut -d: -f6)/.local/share/lp-ranger/update.log"
mkdir -p "$(dirname "$LOG")"

ts() { date '+%F %T'; }
say() { echo "[$(ts)] $*" | tee -a "$LOG"; }

if [[ ! -d "$REPO_DIR/.git" ]]; then
  say "no git clone at $REPO_DIR — nothing to do"
  exit 0
fi

cd "$REPO_DIR"
# Run git as the owner of the repo so we don't dirty file ownership.
sudo -u "$RUN_USER" git fetch --quiet origin || { say "git fetch failed"; exit 1; }
LOCAL=$(sudo -u "$RUN_USER" git rev-parse @)
REMOTE=$(sudo -u "$RUN_USER" git rev-parse '@{u}' 2>/dev/null || echo "$LOCAL")

if [[ "$LOCAL" == "$REMOTE" ]]; then
  say "already up to date ($LOCAL)"
  exit 0
fi

say "update available: $LOCAL → $REMOTE — pulling"
if ! sudo -u "$RUN_USER" git pull --ff-only --quiet; then
  say "git pull (ff-only) refused — local diverges from remote, skipping"
  exit 1
fi

say "running setup_pi.sh to refresh deps + service"
if bash "$REPO_DIR/scripts/setup_pi.sh" >>"$LOG" 2>&1; then
  say "update OK; setup_pi already restarted the service"
else
  say "setup_pi failed; attempting service restart anyway"
  systemctl restart "$SERVICE" || true
fi
