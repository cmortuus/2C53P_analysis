# FNIRSI 2C53 remote scope: read & control a handheld oscilloscope from a computer

Two handheld FNIRSI scopes, one goal: get live waveforms and control onto a computer.

- **2C53T** (project device, Artery AT32F403A): runs [OpenScope-2C53T](https://github.com/DavidClawson/OpenScope-2C53T.git)-based
  open firmware with our `scope dumpbin` streaming command and a stock-FNIRSI-look UI reskin,
  plugged into a **Pi Zero 2 W** that serves a **live browser viewer** (`http://scope-pi.local:8080`:
  both channels at ~20 fps, measurements, controls, cursors, history, spectrum, logger, device-screen
  capture) and a WiFi shell proxy. Firmware can be reflashed from the Pi with no hands on the scope.
- **2C53P** (daily scope, Synwit SWM34S): fully reverse-engineered statically (saved-file format,
  FPGA data path, UART0 factory protocol, config sector) — see `HANDOFF.md`. Not modified.

Start with [`OPERATING.md`](OPERATING.md) (how to drive the scope remotely), [`HANDOFF.md`](HANDOFF.md)
(status, decisions, every finding) and [`T_ARRIVAL_RUNBOOK.md`](T_ARRIVAL_RUNBOOK.md) (backup → flash → recovery).

## Layout
| Path | What |
|------|------|
| `host/pi_bridge/` | Pi server (`scope_server.py`), web viewer (`web/`), systemd unit, install script, cloud-init templates |
| `host/scopectl.py` | **the operator tool**: status, wait-for-signal, autoset, describe (+plot), report, zero/gain calibration, record/replay (post-hoc re-evaluation), set, screen, log — see `OPERATING.md` |
| `host/scope_analysis.py` | the analysis module (levels, edges, frequency, pulse metrics, spectrum, shape, flags, timing) shared by scopectl and the Pi's `/api/analyze`; all criteria are `Params` |
| `host/t_scope.py` | 2C53T streamer/CLI (USB or `--tcp scope-pi.local:7001`): CSV, plot, set commands |
| `host/t_calcheck.py` | calibration sweep against a known generator signal via the viewer API |
| `host/t_calbackup.py` | reads the 2C53T calibration page over the shell and verifies its CRC |
| `patches/openscope-2c53t-dumpbin+ui-fnirsi.patch` | firmware changes (GPL-3, apply to OpenScope-2C53T commit 2eae008): `scope dumpbin` streaming + stock-FNIRSI-look UI reskin. `openscope-2c53t-scope-dumpbin.patch` = streaming only |
| `firmware_builds/` | the exact images flashed (GPL-3 binaries; source = upstream commit + patch) |
| `cal_backup_2c53t/` | this unit's factory calibration page backup (restore before any return) |
| `tools/`, `tools/ghidra/` | 2C53P static-analysis helpers (Ghidra headless scripts, xref/diff tools, `.bin` decoder) |
| `host/fnirsi_uart0.py`, `host/pi_server.py`, `host/client.py` | 2C53P UART0 factory-protocol tools (unused since the 2C53T pivot) |
| `esp32_bridge/` | optional ESP32-S3 USB-host↔WiFi transparent bridge (ESP-IDF; built, untested on hardware) |
| `samples/` | saved waveforms/screens from both scopes |

Not in the repo (see `.gitignore`): downloads, `third_party/` checkouts (clone OpenScope-2C53T and
apply the patch), the Ghidra project, FNIRSI's own firmware, and the Pi cloud-init file that
carries the WiFi password.

## Licensing
Our own code (host tools, Pi server, viewer, ESP32 bridge, analysis scripts): MIT. The firmware
patch and the built images are derived from OpenScope-2C53T and are therefore GPL-3.
