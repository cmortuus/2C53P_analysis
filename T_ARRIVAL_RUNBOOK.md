# 2C53T arrival runbook

Everything is reversible and the case stays closed. Order matters: **backup before any flash.**
Commands are from `/Users/c/2C53P_analysis`. Python = `.venv/bin/python`.

## 0. Unbox
- Charge it a bit, power on **stock** once, let it reach the scope screen (stock sets the
  224 KB-SRAM option byte on first boot). Note the firmware version (Settings → About).
- Use a known **data** USB-C cable to this Mac. Keep any ST-Link/debug probe unplugged.

## 1. Calibration backup (two layers)
1. Enter upgrade mode: device off → hold **MENU**, tap **Power** → an `IAP` drive appears on the Mac.
   ```bash
   .venv/bin/python third_party/OpenScope-2C53T/scripts/iap_flash.py status
   ```
2. Flash the read-only caldump image:
   ```bash
   .venv/bin/python third_party/OpenScope-2C53T/scripts/iap_flash.py flash firmware_builds/openscope-2c53t-guest-caldump-2eae008.bin
   ```
   It boots into a screen showing the 4 KB calibration page summary and **CRC32**. **Photograph it**
   and write the CRC here: `CRC32 = ________`. (Screen may cycle every 5 s.)
3. Flash the working image (next section) and then pull the actual bytes over the shell:
   ```bash
   .venv/bin/python host/t_calbackup.py --port /dev/cu.usbmodem* --expect-crc <CRC from the photo>
   ```
   Output `cal_page_0x08006000_<date>.bin` must report "CRC matches". Copy it somewhere safe too.
   (If the CDC shell doesn't enumerate — a known OpenScope quirk — do this via the Pi later, or rely
   on the photo + `cal backup` from the shell when it does come up.)

> **Check the volume is named `IAP` before flashing.** Plugging in a running stock unit mounts its
> user storage (`NO NAME`, 14.7 MB, folders `LOGO`/`Screenshot file`), and `iap_flash.py status`
> wrongly reports that as "Upgrade mode — ready to flash" (it matches any USB disk < 32 MB).
> Upgrade mode = unit fully OFF, then hold MENU and tap Power.
>
> Never pick `archive/factory_iap_bootloader_2C53T.bin` in the IAP tool's picker — that is the
> bootloader, not an app image. Always pass the image path explicitly.

## 2. Flash OpenScope + streaming command
Device off → MENU + tap Power → `IAP` drive:
```bash
.venv/bin/python third_party/OpenScope-2C53T/scripts/iap_flash.py flash "firmware_builds/openscope-2c53t-guest-coldtrace-2eae008+dumpbin-v3+ui-fnirsi-r15-offsets.bin"   # currently flashed (2026-10-11: + power-off notice + per-unit centred offsets)
```
Expected: it reboots into OpenScope's scope UI with a live trace (the demo trace disappears once
real samples arrive). The factory bootloader is untouched: MENU + Power always works again.

## 3. First contact over USB (Mac)
```bash
ls /dev/cu.usbmodem*                       # the OpenScope CDC port should appear
.venv/bin/python host/t_scope.py --port /dev/cu.usbmodem* --plot
.venv/bin/python host/t_scope.py --port /dev/cu.usbmodem* --set "fpga scope timebase 0x10" --set "help"
```
Probe the generator or a known signal; confirm frequency/amplitude in the plot.
If no `usbmodem` appears within 30 s of boot: that's the CDC enumeration quirk — try a power-cycle
and a different cable; the UI still works regardless.

## 4. Through the Pi (remote)
The Pi Zero 2 W has **one** USB data port, currently used by the USB-Ethernet adapter. To attach
the scope as well you need a small USB 2.0 hub on the OTG adapter (hub → Ethernet + scope), or
WiFi working on the Pi (blocked by the router at the moment). Once the scope is on the Pi:
```bash
ssh pi@10.1.13.114 'ls /dev/serial/by-id/; systemctl status scope-bridge | head -3'
.venv/bin/python host/t_scope.py --tcp 10.1.13.114:7001 --plot
```

## 5. Recovery
- Anything odd after a flash: MENU + Power → `IAP` drive → reflash (either our image or stock).
- Stock firmware (official, verified — sha256 `a17c5c35…c760` matches OpenScope's known-good list):
  ```bash
  .venv/bin/python third_party/OpenScope-2C53T/scripts/iap_flash.py flash "downloads/2c53t_stock/2C53T_Firmware_V1.2.0/2C53T Firmware V1.2.0/APP_2C53T_V1.2.0_251015.bin"
  ```
  (V1.1.2 is also in `downloads/2c53t_stock/` as a fallback.) Note: this wired LAN blocks
  cdn.shopify.com; downloads from FNIRSI go via the WiFi interface (`curl --interface en1`). Calibration page restore needs ROM-DFU (case open) and the
  `.bin` from step 1 — hopefully never.
