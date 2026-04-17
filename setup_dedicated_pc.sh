#!/bin/bash
# ============================================================
# LP Ranger — Setup para PC dedicado
# ============================================================
# Ejecuta este script en un Ubuntu recién instalado.
# Instala todo lo necesario y configura el bot como servicio.
#
# PASOS PREVIOS:
#   1. Formatea el PC con Ubuntu 24.04 Server (o Desktop minimal)
#   2. Conecta a internet (cable o wifi)
#   3. Copia lp-ranger.tar.gz al PC (USB, scp, lo que sea)
#   4. Ejecuta: chmod +x setup_dedicated_pc.sh && sudo ./setup_dedicated_pc.sh
# ============================================================

set -e
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║     LP Ranger — PC Dedicado Setup               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# 1. System update
echo "[1/6] Actualizando sistema..."
apt update -qq && apt upgrade -y -qq
echo "  ✓ Sistema actualizado"

# 2. Install dependencies
echo "[2/6] Instalando dependencias..."
apt install -y -qq python3 python3-pip nodejs npm curl git
pip3 install web3 --break-system-packages
echo "  ✓ Python + web3 instalados"

# 3. Install Claude Code
echo "[3/6] Instalando Claude Code..."
npm install -g @anthropic-ai/claude-code 2>/dev/null || echo "  ⚠ Claude Code requiere configuración manual (ver docs.anthropic.com)"
echo "  ✓ Claude Code instalado (configúralo con: claude login)"

# 4. Setup LP Ranger
echo "[4/6] Instalando LP Ranger..."
INSTALL_DIR="/opt/lp-ranger"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR"/*.json "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR/icons" "$INSTALL_DIR/" 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.py
echo "  ✓ Archivos copiados a $INSTALL_DIR"

# 5. Create user for the bot (security: no root)
echo "[5/6] Creando usuario lpranger..."
id -u lpranger &>/dev/null || useradd -r -m -s /bin/bash lpranger
mkdir -p /home/lpranger/.local/share/lp-ranger
chown -R lpranger:lpranger /home/lpranger
chown -R lpranger:lpranger "$INSTALL_DIR"
echo "  ✓ Usuario lpranger creado"

# 6. Create systemd service
echo "[6/6] Creando servicio systemd..."

read -p "  Position ID (NFT number): " POSITION_ID
echo ""
echo "  Ahora configura la wallet. Se ejecutará como usuario lpranger."
echo "  Escribe la clave privada y un password para cifrarla."
echo ""
su - lpranger -c "python3 $INSTALL_DIR/lp_autobot.py --setup"

read -sp "  Repite el password (se guardará cifrado para auto-unlock): " BOT_PASSWORD
echo ""
echo "$BOT_PASSWORD" > /home/lpranger/.lp-password
chmod 600 /home/lpranger/.lp-password
chown lpranger:lpranger /home/lpranger/.lp-password

cat > /etc/systemd/system/lp-ranger.service << EOF
[Unit]
Description=LP Ranger - Automated LP Management
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lpranger
Group=lpranger
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/lp_daemon.py -p $POSITION_ID --with-claude --password-file /home/lpranger/.lp-password
Restart=always
RestartSec=30
StandardOutput=append:/var/log/lp-ranger.log
StandardError=append:/var/log/lp-ranger.log

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/home/lpranger/.local/share/lp-ranger
ReadWritePaths=/var/log/lp-ranger.log

[Install]
WantedBy=multi-user.target
EOF

touch /var/log/lp-ranger.log
chown lpranger:lpranger /var/log/lp-ranger.log

systemctl daemon-reload
systemctl enable lp-ranger

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  ✓ Todo instalado!                              ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║                                                  ║"
echo "║  Iniciar:    systemctl start lp-ranger           ║"
echo "║  Parar:      systemctl stop lp-ranger            ║"
echo "║  Estado:     systemctl status lp-ranger          ║"
echo "║  Logs live:  tail -f /var/log/lp-ranger.log      ║"
echo "║  Logs:       journalctl -u lp-ranger -f          ║"
echo "║                                                  ║"
echo "║  El bot arranca automáticamente con el PC.       ║"
echo "║  Comprueba los logs para ver que funciona.       ║"
echo "║                                                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
read -p "  ¿Iniciar ahora? [s/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Ss]$ ]]; then
    systemctl start lp-ranger
    echo "  ✓ Bot en marcha. Comprueba: tail -f /var/log/lp-ranger.log"
fi
