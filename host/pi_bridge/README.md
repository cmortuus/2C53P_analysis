# Pi Zero 2 W bridge for the 2C53T: live web viewer + WiFi shell proxy

The Pi is the USB host for the scope. `scope_server.py` (stdlib + pyserial, no other deps) owns
the scope's CDC shell and offers:

| Port | What | Use |
|------|------|-----|
| **8080** | Live viewer (`web/index.html`) + JSON API | `http://scope-pi.local:8080` in any browser |
| **7001** | Raw shell proxy (same as the old socat bridge) | `host/t_scope.py --tcp scope-pi.local:7001`, `nc scope-pi.local 7001` |

Both share one serial lock, so the viewer and a `t_scope.py` session can coexist (commands are
serialised; the viewer's acquisition loop simply yields between records).

## Viewer
Both channels live (SSE push, ~20 fps with firmware v3; the raw record takes ~30 ms on the Pi), FNIRSI-style
status bar (V/div, probe, coupling, timebase, trigger), 10×8 graticule with volt/time scaling
from the record header + OpenScope's calibration tables, measurements (Vpp/Vmax/Vmin/mean/
AC-RMS/freq/period/duty, clipping warning), controls (timebase, V/div per channel, trigger
mode/edge/level, RUN/STOP, PNG + CSV download) and a shell box limited to an allow-list.

API: `GET /api/info` (calibration tables, port, status), `GET /api/stream` (SSE, one JSON frame
per event with base64 `ch1`/`ch2`), `GET /api/frame.json`, `GET /api/frame.csv`,
`POST /api/cmd {"cmd": "..."}` (allow-list: `fpga scope …`, `btn …` except POWER, `version`,
`status`, `settings`, `help`; **never** `mem write`, `fwload`, flash or reinit commands),
`POST /api/run {"run": bool}`.

## Install / update
```bash
scp -r host/pi_bridge pi@scope-pi.local:            # from the Mac
ssh pi@scope-pi.local sudo bash pi_bridge/install.sh
```
Installs `python3-serial` + `mtools`, copies to `/opt/scope`, enables `scope-server.service`
(replaces the old `scope-bridge` socat unit). Logs: `journalctl -u scope-server -f`.

## Flashing firmware from the Pi
**No hands needed (proven 2026-10-10):** `sudo bash /home/pi/remote_flash.sh <image.bin>` stages the
image over the shell (`fwload`, CRC-checked, ~95 s) and installs it (`fwswap b`, RAM installer +
system reset), then re-authorises the USB device and restarts the viewer. Only run it FROM a
firmware that OpenScope's `scripts/test_ramfunc_isolated.py --check-bin` reports ok.

**Fallback (needs someone at the scope):** IAP flash with the scope in upgrade mode:
```bash
scp firmware_builds/<image>.bin third_party/OpenScope-2C53T/scripts/iap_flash.py pi@scope-pi.local:
ssh pi@scope-pi.local 'sudo systemctl stop scope-server'
# on the scope: USB connected, hold MENU + pinhole reset (or tap Power from off) -> "Firmware upgrade"
ssh pi@scope-pi.local 'python3 iap_flash.py status && sudo python3 iap_flash.py flash <image>.bin'
ssh pi@scope-pi.local 'sudo systemctl start scope-server'
```

## Reference signal for calibration checks
`config.txt` has `dtoverlay=pwm,pin=18,func=2`; the server does not touch it. Hardware PWM on
GPIO18 (header pin 12, outer row, 6th from the SD-card end; GND = pin 6, 3rd) from the Pi's crystal:
```bash
C=/sys/class/pwm/pwmchip0; echo 0 | sudo tee $C/export; echo 1000000 | sudo tee $C/pwm0/period   # ns
echo 500000 | sudo tee $C/pwm0/duty_cycle; echo 1 | sudo tee $C/pwm0/enable
```
Then `host/t_calcheck.py --freq 1000 --vpp 3.3 --wave square --ch 1`. Verified 2026-10-10: all
measured timebase codes within ±0.2 %, 0x0D is really 125 kS/s (override in `scope_server.py`).

## Hardware / mounting
- Pi Zero 2 W, micro-SD, **micro-USB OTG adapter** on the port marked `USB` (inner one; `PWR IN`
  is the outer one), USB-A→USB-C cable to the scope.
- Power the Pi from `PWR IN` (5 V ≥ 1 A). The scope's USB-C is a *device* port and cannot power
  the Pi. For a strapped-on build: one 2-port USB power bank/wall brick, short OTG + USB-C
  cables, the Pi in a slim case velcroed to the scope's back. Idle draw of the Pi ≈ 0.6 W.
- Unplug the USB-Ethernet adapter once WiFi is confirmed; everything runs over wlan0.

## One-time OS setup (done 2026-10-09/10; keep for re-imaging)
1. Image the card headless with the cloud-init files in `cloudinit/` (edit the WiFi placeholders
   in `network-config` first — that file is git-ignored because it holds the real password;
   `user-data` carries the Mac's SSH key, user `pi`, SSH-key only, passwordless sudo):
   ```bash
   "/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager" --cli \
     --cloudinit-userdata host/pi_bridge/cloudinit/user-data \
     --cloudinit-networkconfig host/pi_bridge/cloudinit/network-config \
     downloads/rpios/2026-10-06-raspios-trixie-arm64-lite.img.xz /dev/diskN   # N = the SD card!
   ```
2. Then apply the WiFi fix below (cloud-init's NetworkManager profile will *not* connect here).
3. `scp -r host/pi_bridge pi@scope-pi.local: && ssh pi@scope-pi.local sudo bash pi_bridge/install.sh`.

## Network topology at the owner's (2026-10-10)
- Mac and Pi both on the NATed WiFi/wired segment **10.10.12.0/24** (gateway 10.10.12.1, upstream
  192.168.1.254). Pi wlan0 = 10.10.12.222 (`scope-pi.local` via mDNS).
- The wired LAN firewall blocks some CDNs (e.g. cdn.shopify.com); use `curl --interface en1`.

## WiFi FIXED (2026-10-10)
Root cause: "<home WiFi>" is WPA2/WPA3 transition mode. NetworkManager 1.52 silently adds SAE (WPA3)
to a `wpa-psk` profile there, the supplicant prefers SAE, and the Zero 2 W's BCM43430 can't do it
(firmware-offloaded SAE, AP answers `ASSOC-REJECT status_code=16`). Plain WPA2-PSK works.
Fix on the Pi (survives reboot, verified):
- `/etc/NetworkManager/conf.d/90-wlan0-unmanaged.conf` — NM leaves wlan0 alone (eth0 still NM).
- `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (mode 600, hashed PSK) — `key_mgmt=WPA-PSK`,
  `ieee80211w=0`; service `wpa_supplicant@wlan0` enabled.
- `/etc/systemd/network/30-wlan0.network` — DHCP on wlan0 via `systemd-networkd` (enabled;
  `systemd-networkd-wait-online` masked so boot isn't delayed).
- `/etc/NetworkManager/conf.d/91-dns-static.conf` (`dns=none`) + static `/etc/resolv.conf`
  (10.10.12.1, 1.1.1.1) so DNS works on WiFi-only.
- Kernel cmdline also has `brcmfmac.feature_disable=0x82000` (disables firmware SAE/supplicant
  offload; harmless; backup `cmdline.txt.bak-sae`). Not sufficient on its own.
To change WiFi network: `wpa_passphrase SSID PASS` into the wlan0 supplicant file, then
`sudo systemctl restart wpa_supplicant@wlan0`.

## Gotchas learned the hard way (2026-10-09)
- **cloud-init applies `network-config`/`user-data` only on the first boot of an instance-id.** Editing
  the files on `bootfs` after a boot does nothing until you change `instance-id` in `meta-data` AND the
  `i=…` value in `cmdline.txt` (same string).
- **A wrong first WiFi config persists even after fixing it:** NetworkManager writes the connection it
  tried as `/etc/netplan/90-NM-<uuid>.yaml`, which overrides later cloud-init renders. Delete it from
  the Linux partition (`sudo debugfs -w -R 'rm /etc/netplan/90-NM-….yaml' /dev/rdiskNs2`) and bump
  the instance-id again.
- Logs can be read off the card on a Mac without booting the Pi:
  `brew install e2fsprogs`, then `sudo debugfs -R 'cat /var/log/cloud-init-output.log' /dev/rdiskNs2`.
- Solid green LED with no activity = no SD card detected. The Zero 2 W's USB gadget mode never
  enumerated on this Mac (cable/OTG); use WiFi or a USB-Ethernet adapter instead.
- The scope's USB enumerates flakily (OpenScope known issue); the server waits and retries every
  second for `/dev/serial/by-id/*Artery*`.

## Legacy
`scope-bridge.sh` is the old socat-only bridge (port 7001, no viewer); `install.sh` now disables it.
