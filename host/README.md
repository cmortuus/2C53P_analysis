# 2C53P host tools (UART0 link)

`fnirsi_uart0.py` — protocol library + local monitor (works directly on the Mac with a USB-UART adapter).
`pi_server.py` — runs on the Pi Zero strapped to the scope; bridges UART0 to TCP/JSON.
`client.py` — Mac-side viewer/controller for the Pi server.

Dependencies: Python 3.9+ and `pyserial` (`pip install pyserial`). Nothing else.

## Wiring (3.3 V logic — never 5 V)

Main-board 7-pin connector, silkscreen order: `BUZZ_CHK · 3V3 · GND · 5V · TX · RX · 3V3_EN`
(the `TX`/`RX` pads under the MCU on the bottom side are the same net, if the connector is awkward).

| Scope pin | USB-UART adapter | Pi Zero (GPIO header)        |
|-----------|------------------|------------------------------|
| GND       | GND              | pin 6 (GND)                  |
| TX        | RXD              | pin 10 (GPIO15 / RXD)        |
| RX        | TXD              | pin 8  (GPIO14 / TXD)        |

Leave `3V3`, `5V`, `BUZZ_CHK`, `3V3_EN` unconnected. Scope TX is the scope's output; connect it only
to an input. If in doubt, connect **TX→RXD and GND only** first: that is enough to see the boot burst.

## Step 1 — prove it's UART0 (Mac + USB-UART adapter)

```bash
pip install pyserial
python3 host/fnirsi_uart0.py --port /dev/cu.usbserial-XXXX --no-ack --log uart0_boot
```
Power-cycle the scope. Expected: the same `55 xx … 0A` frame repeating (the scope resends its first
boot notification until a host echoes it). If you see that, run again **without** `--no-ack`: the tool
echoes each notification, the scope drains its 22 boot notifications and from then on reports every
settings change you make on the touchscreen (timebase, V/div, positions, channel on/off…). That log
is also how we name the still-unidentified commands: change one thing, see which `cmd` fires.

If nothing arrives at 921600, try 115200 (`BAUD` in `fnirsi_uart0.py`) before concluding it's not UART0.

## Step 2 — Pi Zero bridge

1. Raspberry Pi OS Lite. In `/boot/firmware/config.txt` (or `/boot/config.txt`) add
   `enable_uart=1` and `dtoverlay=disable-bt`, and remove `console=serial0,...` from `cmdline.txt`
   so the PL011 UART (`/dev/ttyAMA0`) is free and can do 921600 reliably (the mini-UART can't).
   Alternatively plug the USB-UART adapter into the Pi's USB OTG port and use `/dev/ttyUSB0`.
2. `sudo apt install python3-serial`; copy `host/fnirsi_uart0.py` and `host/pi_server.py` to `~/scope/`.
3. Test: `python3 ~/scope/pi_server.py --port /dev/ttyAMA0 --listen 0.0.0.0:7000`
4. Autostart — `/etc/systemd/system/scope-bridge.service`:
   ```ini
   [Unit]
   Description=2C53P UART0 bridge
   After=network.target
   [Service]
   ExecStart=/usr/bin/python3 /home/pi/scope/pi_server.py --port /dev/ttyAMA0 --listen 0.0.0.0:7000
   Restart=always
   User=pi
   [Install]
   WantedBy=multi-user.target
   ```
   `sudo systemctl enable --now scope-bridge`.
5. From the Mac: `python3 host/client.py --pi <pi-hostname>.local:7000`.

Power: the scope's `5V` pin is its USB/battery rail; powering the Pi from it is tempting but untested —
use the Pi's own USB power for now.

## What this gives today vs. later

- **Today (stock firmware):** live settings monitor (every change the scope makes), read-back of
  calibration bytes and boot ID, and *answer-based* control: when the scope queries a value (`AA` frame)
  the bridge can reply with one you chose (`client.py --answer 3=7`). No waveform data on this link.
- **Later (firmware patch):** the same bridge carries the 461-sample frames; the TCP protocol already
  streams arbitrary events so the client side won't change shape.

## Protocol summary
Frame: `hdr cmd data… cksum 0A`, `cksum = (hdr+cmd+Σdata) & 0xFF`. `hdr=55` notification → host echoes
it back as ACK. `hdr=AA` query → host answers `AA cmd value cksum 0A` and the scope applies it as a SET.
Command table in `fnirsi_uart0.py`; firmware details in `../HANDOFF.md`.
