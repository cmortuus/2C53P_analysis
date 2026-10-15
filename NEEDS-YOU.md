# Needs the owner

One line each: what, why, what it unblocks. Remove when done.

- **Connect the scope to the Pi and turn it on:** the scope isn't on the Pi's USB (as of 2026-10-14 21:25). Use a normal data cable from the Pi's "USB" port with the 5 V wire intact, and power the Pi from its own supply. Unblocks: charging the battery (2.98 V when last seen), and the first hardware test of probe-touch capture and the battery monitor.
- **Delete the old private repo `cmortuus/2C53P_analysis-old-private`** (github.com → repo → Settings → Delete). It holds the pre-scrub history (your name and email on every commit). The assistant's GitHub login can't delete repos. Unblocks: nothing; it's cleanup.
- **Model numbers when the Mesa card arrives:** the Mesa card, the stepper/servo drives, the VFD, and whether the limit switches are NO or NC / NPN or PNP (a photo of the labels is fine). Unblocks: real datasheet limits for `scopectl cnc step` / `levels` / `bounce` instead of generic defaults (CNC_BRINGUP.md).
- **~1 V divider (22k/10k) on the reference** for range 4 on both channels (and CH2 range 5, which clips at 3.19 V even with the offset shifted). Unblocks: those gain calibrations.
- **Internal build: photos of the opened board** before any soldering. Unblocks: confirming the D+/D-, 3V3 and battery solder points (INTERNAL_BUILD.md).
