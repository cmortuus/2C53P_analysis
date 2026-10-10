#!/bin/bash
# Install the 2C53T live viewer + raw shell proxy (scope_server.py) as a systemd service.
# Run on the Pi with sudo from inside this folder:  sudo bash install.sh
# Replaces the older socat-only scope-bridge service (same port 7001 for t_scope.py --tcp).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
apt-get update -qq && apt-get install -y -qq python3-serial mtools
install -d /opt/scope/web
install -m 644 "$HERE/scope_server.py" /opt/scope/
install -m 644 "$HERE/web/index.html" /opt/scope/web/
install -m 644 "$HERE/scope-server.service" /etc/systemd/system/
usermod -aG dialout pi 2>/dev/null || true
systemctl disable --now scope-bridge 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now scope-server
systemctl restart scope-server
sleep 2
systemctl --no-pager status scope-server | head -5
echo "Viewer: http://$(hostname).local:8080   raw shell: $(hostname).local:7001"
