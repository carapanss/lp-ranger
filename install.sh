#!/bin/bash
# ============================================================
# LP Ranger — Installer for Ubuntu
# ============================================================
set -e

APP_NAME="LP Ranger"
INSTALL_DIR="$HOME/.local/share/lp-ranger/app"
AUTOSTART_DIR="$HOME/.config/autostart"
BIN_DIR="$HOME/.local/bin"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  $APP_NAME — Instalador"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. Install system dependencies
echo "[1/5] Instalando dependencias del sistema..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 libnotify-bin 2>/dev/null || \
sudo apt-get install -y -qq python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    gir1.2-appindicator3-0.1 libnotify-bin 2>/dev/null
echo "  ✓ Dependencias instaladas"

# 2. Create install directory
echo "[2/5] Copiando archivos..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/icons"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/lp_ranger.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/strategy_v1.json" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/strategy_aggressive.json" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/strategy_exit_pool.json" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/lp_autobot.py" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/lp_bridge.py" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/lp_daemon.py" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/icons/"*.png "$INSTALL_DIR/icons/"
chmod +x "$INSTALL_DIR/lp_ranger.py"
echo "  ✓ Archivos copiados a $INSTALL_DIR"

# 3. Create launcher script
echo "[3/5] Creando lanzador..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/lp-ranger" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/lp_ranger.py" "\$@"
EOF
chmod +x "$BIN_DIR/lp-ranger"

# Autobot launcher
cat > "$BIN_DIR/lp-autobot" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/lp_autobot.py" "\$@"
EOF
chmod +x "$BIN_DIR/lp-autobot"

# Bridge launcher
cat > "$BIN_DIR/lp-bridge" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/lp_bridge.py" "\$@"
EOF
chmod +x "$BIN_DIR/lp-bridge"

# Daemon launcher
cat > "$BIN_DIR/lp-daemon" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/lp_daemon.py" "\$@"
EOF
chmod +x "$BIN_DIR/lp-daemon"

echo "  ✓ Lanzadores creados en $BIN_DIR/"

# 4. Create autostart entry
echo "[4/5] Configurando autostart..."
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/lp-ranger.desktop" << EOF
[Desktop Entry]
Type=Application
Name=LP Ranger
Comment=Monitor de Liquidity Pool WETH/USDC
Exec=$BIN_DIR/lp-ranger
Icon=$INSTALL_DIR/icons/green_48.png
Terminal=false
Categories=Utility;Finance;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF
echo "  ✓ Autostart configurado"

# 5. Create .desktop file for app menu
echo "[5/5] Añadiendo a menú de aplicaciones..."
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/lp-ranger.desktop" << EOF
[Desktop Entry]
Type=Application
Name=LP Ranger
Comment=Monitor de Liquidity Pool WETH/USDC
Exec=$BIN_DIR/lp-ranger
Icon=$INSTALL_DIR/icons/green_48.png
Terminal=false
Categories=Utility;Finance;
StartupNotify=false
EOF
echo "  ✓ Añadido al menú"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ $APP_NAME instalado correctamente!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Para iniciar:  lp-ranger"
echo "  Con position:  lp-ranger --position-id 4950738"
echo "  Con rango:     lp-ranger --range-lo 2143 --range-hi 2475"
echo ""
echo "  AutoBot:       lp-autobot --setup          (configurar clave)"
echo "                 lp-autobot --status -p ID    (ver estado)"
echo ""
echo "  Bridge+Claude: lp-bridge --daemon           (modo automático)"
echo "                 lp-bridge --test              (probar)"
echo ""
echo "  Daemon (VPS):  lp-daemon -p ID --with-claude (24/7 headless)"
echo "                 lp-daemon --setup-vps          (guía VPS)"
echo ""
echo "  La app arrancará automáticamente con Ubuntu."
echo "  Para desinstalar: rm -rf $INSTALL_DIR $BIN_DIR/lp-ranger"
echo "                    rm $AUTOSTART_DIR/lp-ranger.desktop"
echo ""

# Offer to start now
read -p "  ¿Iniciar LP Ranger ahora? [s/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Ss]$ ]]; then
    echo "  Iniciando LP Ranger..."
    nohup "$BIN_DIR/lp-ranger" &>/dev/null &
    echo "  ✓ LP Ranger ejecutándose en segundo plano"
fi
