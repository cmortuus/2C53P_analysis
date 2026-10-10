# scope_bridge — ESP32-S3 USB-CDC ↔ WiFi TCP bridge

Transparent bridge: the OpenScope-2C53T shell (USB CDC) becomes a TCP socket on the LAN.
`host/t_scope.py --tcp <ip>:7001` then works exactly like `--port /dev/cu.usbmodem*`.

## Hardware
ESP32-S3-DevKitC-1 (N16R8 or any). Two USB-C ports:
- **UART** port: powers the board and is used for flashing/logs.
- **USB** port (native, GPIO19/20): host port — plug the **scope** in here with a C-to-C cable.
  The scope needs 5 V on VBUS to enumerate: close the board's `USB-OTG` / `IN-OUT` solder
  pad (official DevKitC-1 silkscreen), or wire the `5V` pin to the USB connector's VBUS.

## Build (first time)
```bash
export IDF_TOOLS_PATH=/Users/c/2C53P_analysis/downloads/esp/tools
. /Users/c/2C53P_analysis/downloads/esp/esp-idf/export.sh
cd /Users/c/2C53P_analysis/esp32_bridge
idf.py set-target esp32s3
idf.py menuconfig          # Scope bridge -> WiFi SSID / password (or edit sdkconfig)
idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor     # the UART port
```
The log prints the IP once associated. Then on the Mac:
```bash
.venv/bin/python host/t_scope.py --tcp 192.168.x.y:7001 --plot
```

## Notes
- No protocol knowledge in the bridge: every shell command works remotely, including control
  (`fpga scope timebase …`), `screen dumpbin`, firmware staging, etc.
- Up to 4 TCP clients; all receive everything the scope sends; all may write.
- DTR is asserted on open (some CDC shells gate output on it).
- WiFi credentials live in `sdkconfig` (gitignored by ESP-IDF convention); keep `sdkconfig.defaults` clean.
