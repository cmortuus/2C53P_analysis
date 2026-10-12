# Needs the owner

One line each: what, why, what it unblocks. Remove when done.

- **Move the Pi for better WiFi.** The link is 20-77 % packet loss (worse after the 2026-10-12 rotation; it was 7-30 % before, -69 dBm). Try the old orientation, or move it closer to / in line of sight with the router, away from metal and cables. Unblocks: a reliable live viewer and remote control (local recording is unaffected).
- **CH2 probe on the second reference pin** (outer row, 16th hole from the SD end = pin 32 / GPIO12; ground with CH1's). Unblocks: CH2 calibration on ranges 5, 8, 9.
- **~1 V divider (22k/10k) on the reference** for range 4 on both channels. Unblocks: range 4 gain calibration.
- **Push the repo to GitHub** (the assistant is not allowed to push). Unblocks: an off-machine backup.
- **Internal build: photos of the opened board** before any soldering. Unblocks: confirming the D+/D-, 3V3 and battery solder points (INTERNAL_BUILD.md).
