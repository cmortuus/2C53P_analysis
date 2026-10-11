#!/bin/bash
# Install the 2C53T live viewer + raw shell proxy (scope_server.py) as a systemd service.
# Run on the Pi with sudo from inside this folder:  sudo bash install.sh
# Replaces the older socat-only scope-bridge service (same port 7001 for t_scope.py --tcp).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
apt-get update -qq && apt-get install -y -qq python3-serial python3-numpy mtools
install -d /opt/scope/web /home/pi/records
install -m 644 "$HERE/scope_server.py" /opt/scope/
install -m 644 "$HERE/../scope_analysis.py" /opt/scope/      # shared analysis (server-side /api/analyze)
chown -R pi /opt/scope /home/pi/records                       # calib.json and raw records are written by the service
install -m 644 "$HERE/web/index.html" /opt/scope/web/
install -m 644 "$HERE/scope-server.service" "$HERE/pwm-ref.service" /etc/systemd/system/
grep -q "dtoverlay=pwm,pin=18" /boot/firmware/config.txt || printf "\n# hardware PWM reference on GPIO18 for calibration checks\ndtoverlay=pwm,pin=18,func=2\n" >> /boot/firmware/config.txt
usermod -aG dialout,gpio pi 2>/dev/null || true     # gpio: VBUS sense for the internal build
systemctl disable --now scope-bridge 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now scope-server
systemctl enable --now pwm-ref 2>/dev/null || echo "pwm-ref: needs the dtoverlay (reboot once)"
systemctl restart scope-server
sleep 2
systemctl --no-pager status scope-server | head -5
echo "Viewer: http://$(hostname).local:8080   raw shell: $(hostname).local:7001"
