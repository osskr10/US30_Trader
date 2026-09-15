#!/usr/bin/env bash
# Deploy de us30_trader al VPS (188.34.161.113), espejando el patrón de us30_alerts.
# Reusa el venv de /opt/us30_alerts. Corre DESDE la Mac.
#
#   bash deploy/deploy_vps.sh
#
# Copia el proyecto (sin .venv/state/__pycache__), instala el systemd unit, y
# arranca el servicio. El .env (secrets) viaja por scp cifrado.
set -euo pipefail

VPS="root@188.34.161.113"
LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="/opt/us30_trader"
TARBALL="/tmp/us30_trader_deploy.tgz"

echo "==> Empaquetando $LOCAL (sin .venv/state/__pycache__)"
tar czf "$TARBALL" -C "$LOCAL" \
  --exclude='.venv' --exclude='state' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.git' .

echo "==> Copiando al VPS"
scp -o ConnectTimeout=15 "$TARBALL" "$VPS:/tmp/"

echo "==> Instalando en $REMOTE + systemd"
ssh -o ConnectTimeout=15 "$VPS" bash -s <<'REMOTE_EOF'
set -euo pipefail
mkdir -p /opt/us30_trader/state
tar xzf /tmp/us30_trader_deploy.tgz -C /opt/us30_trader
chown -R alerts:alerts /opt/us30_trader
chmod 600 /opt/us30_trader/.env 2>/dev/null || true

install -m 644 /opt/us30_trader/deploy/us30-trader.service /etc/systemd/system/us30-trader.service
systemctl daemon-reload
systemctl enable us30-trader
systemctl restart us30-trader
sleep 3
echo "---- status ----"
systemctl status us30-trader --no-pager || true
echo "---- últimos logs ----"
journalctl -u us30-trader -n 25 --no-pager || true
rm -f /tmp/us30_trader_deploy.tgz
REMOTE_EOF

rm -f "$TARBALL"
echo "==> Deploy terminado."
