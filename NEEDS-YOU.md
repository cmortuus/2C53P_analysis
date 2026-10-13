# Needs the owner

One line each: what, why, what it unblocks. Remove when done.

- **OK to rewrite git history before going public?** All 48 earlier commits carry your full name + gmail as author, and old file versions contain the WiFi name, AP/Pi MAC addresses and the scope serial (no passwords anywhere). Current files are scrubbed (uncommitted). Plan: rewrite every commit (author -> GitHub no-reply, same text scrub), force-push to the private repo, re-audit, then make it public. Original history backed up locally in `downloads/git-backup-before-public-2026-10-12.bundle` (never pushed). The tool's safety check blocks this without your explicit OK. Unblocks: public release.
- **Model numbers when the Mesa card arrives:** the Mesa card, the stepper/servo drives, the VFD, and whether the limit switches are NO or NC / NPN or PNP (a photo of the labels is fine). Unblocks: real datasheet limits for `scopectl cnc step` / `levels` / `bounce` instead of generic defaults (CNC_BRINGUP.md).
- **~1 V divider (22k/10k) on the reference** for range 4 on both channels (and CH2 range 5, which clips at 3.19 V even with the offset shifted). Unblocks: those gain calibrations.
- **Internal build: photos of the opened board** before any soldering. Unblocks: confirming the D+/D-, 3V3 and battery solder points (INTERNAL_BUILD.md).
