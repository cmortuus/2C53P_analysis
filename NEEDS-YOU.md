# Needs the owner

One line each: what, why, what it unblocks. Remove when done.

- **Model numbers when the Mesa card arrives:** the Mesa card, the stepper/servo drives, the VFD, and whether the limit switches are NO or NC / NPN or PNP (a photo of the labels is fine). Unblocks: real datasheet limits for `scopectl cnc step` / `levels` / `bounce` instead of generic defaults (CNC_BRINGUP.md).
- **CH2 probe on the second reference pin** (outer row, 16th hole from the SD end = pin 32 / GPIO12; ground with CH1's). Unblocks: CH2 calibration on ranges 5, 8, 9.
- **~1 V divider (22k/10k) on the reference** for range 4 on both channels. Unblocks: range 4 gain calibration.
- **Push the repo to GitHub** (the assistant is not allowed to push). Unblocks: an off-machine backup.
- **Internal build: photos of the opened board** before any soldering. Unblocks: confirming the D+/D-, 3V3 and battery solder points (INTERNAL_BUILD.md).
