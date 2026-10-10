#!/bin/bash
# Reflash the 2C53T from the Pi with no hands on the scope, via OpenScope's fwload/fwswap path.
#   sudo bash remote_flash.sh <image.bin>        (image linked for the app slot: make guest-*)
# Steps: stop the viewer, stage the image into W25Q cache slot b (CRC-verified, harmless),
# 'fwswap b' = RAM-resident installer erases/programs the app slot and system-resets, wait
# for the CDC port, re-authorise the USB device (the Pi's cdc_acm keeps a stale port after
# the reset -- writes time out until it re-enumerates), restart the viewer, print 'version'.
# Recovery if the scope never comes back: hold MENU through a pinhole reset -> IAP drive ->
#   python3 iap_flash.py flash <known-good.bin>     (images + iap_flash.py live in /home/pi)
# Safety: only fwswap FROM an image that scripts/test_ramfunc_isolated.py --check-bin calls ok
# (v3 is ok, verified 2026-10-10). Proven round trip 2026-10-10 18:30.
set -e
IMG="$1"; [ -f "$IMG" ] || { echo "usage: $0 <image.bin>"; exit 1; }
PORT=/dev/ttyACM0
systemctl stop scope-server
sleep 1
python3 /home/pi/cdc_flash.py "$IMG" --port $PORT --slot b --stage-only | tail -4
python3 - <<PY
import serial, time
s = serial.Serial("$PORT", 115200, timeout=0.2, write_timeout=2)
s.write(b"\r"); time.sleep(0.3); s.reset_input_buffer()
s.write(b"fwswap b\r"); t0 = time.time(); out = b""
while time.time() - t0 < 30:
    try:
        c = s.read(4096)
    except Exception as e:
        print("port gone (expected: device reset):", e); break
    out += c
print(out.decode(errors="replace")[-400:])
PY
echo "waiting for the scope to re-enumerate..."
for i in $(seq 1 60); do [ -e $PORT ] && break; sleep 2; done
sleep 3
D=$(readlink -f /sys/bus/usb/devices/1-1)
echo 0 > "$D/authorized"; sleep 2; echo 1 > "$D/authorized"; sleep 5
python3 - <<PY
import serial, time
s = serial.Serial("$PORT", 115200, timeout=0.5, write_timeout=2)
s.write(b"\r"); time.sleep(0.4); s.reset_input_buffer()
s.write(b"version\r"); time.sleep(1.5); print(s.read(4000).decode(errors="replace"))
PY
systemctl start scope-server
echo "done"
