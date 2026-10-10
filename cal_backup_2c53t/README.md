# 2C53T calibration page backup (MCU flash 0x08006000..0x08006FFF)

Read 2026-10-10 with OpenScope `guest-caldump` (read-only) on the newly arrived unit, stock FW before.

- Status: POPULATED - data present
- non-FF: 513 / 4096 bytes, range 0x000 .. 0x803 (256 B chunks used: 0, 1, 8)
- **CRC32: 1ADD539B** (poly 0xEDB88320, init/xorout 0xFFFFFFFF over all 4096 bytes)
- vs OpenScope reference unit: DIFFERS 163 B in 0x003..0x12E — hdr/settings 6/48, CALIBRATION 157/256,
  tail 0/208 → this unit has its own factory calibration.

Photos: `caldump_summary_CRC_1ADD539B.jpg`, `caldump_chunk00*.jpg`.
Byte-exact copy: `cal_page_0x08006000_<date>.bin` from `host/t_calbackup.py --expect-crc 1ADD539B`
(captured 2026-10-10 16:00 over the OpenScope shell — CRC32 1ADD539B MATCHES the screen; read-only; second copy ~/Documents/2C53T_cal_page_0x08006000_CRC_1ADD539B.bin).
Restore (only if ever needed): ROM DFU, `dfu-util -a 0 -d 2e3c:df11 -s 0x08006000 -D cal_page.bin`.
