# Operating the 2C53T remotely — guide for the assistant (and anyone driving it blind)

Purpose (the owner, 2026-10-10): "I want to be able to hook up wires and have you do the rest" —
e.g. debugging the CNC without snapping pictures of the screen or a back-and-forth about settings.
So the assistant sets up the scope, takes the measurements, judges whether they can be trusted,
and explains in plain language. The owner only connects probes.

## The one tool: `host/scopectl.py` (runs on the Mac, talks to the Pi's API)
```bash
P=.venv/bin/python
$P host/scopectl.py health                       # whole chain: link, fps, firmware, recorder, index, disk, Pi load -> ALL OK?
$P host/scopectl.py status                       # link, settings, both channels at a glance
$P host/scopectl.py wait --ch any --timeout 300  # block until a probe sees something (ask the owner to hook up, then run this)
$P host/scopectl.py autoset --ch 2 --png /tmp/a.png   # finest non-clipping range, ~10 cycles, mid-level trigger
$P host/scopectl.py describe --png /tmp/d.png    # both channels: shape, freq, levels, flags + a plot to LOOK at
$P host/scopectl.py describe --ch 1 --json       # machine-readable
$P host/scopectl.py set "fpga scope timebase 0x10"   # any allow-listed shell command
$P host/scopectl.py screen /tmp/lcd.png          # the device's own LCD
$P host/scopectl.py log start | stop | csv out.csv [--summary]   # long recordings (1 row per frame, hours)
$P host/scopectl.py autoset --ch both --png /tmp/a.png   # both channels; timebase follows the faster one (step, not dir)
$P host/scopectl.py report /tmp/r1 --title "spindle PWM"   # ONE call: autoset both, describe, timing, plot, LCD, report.md + raw.json
$P host/scopectl.py zero --ch 2                  # tip on GROUND first: stores this range's zero offset on the Pi
$P host/scopectl.py gain --ch 2 --expected 3.3   # a known amplitude applied: stores this range's gain
$P host/scopectl.py calib show | clear --ch 2 --rng 6
```
`describe` also lists the three strongest spectral peaks (dB relative to the strongest — a square shows
its odd harmonics at −10/−14 dB; VFD/switching noise shows up here) and warns when a level is within
20 % of the input limit for the probe setting. For pulse trains it reports high/low widths, 10–90 % rise/fall (or "unresolved" when the
edge is under 2 samples), runt-pulse count (glitches, contact bounce); and with both channels on the
same frequency the CH1→CH2 delay/phase, flagging ~90° as quadrature (encoder A/B order).
Always read the plot (Read tool on the PNG). The text summary is good, the plot catches what
the summary cannot (glitches, ringing, asymmetry, a probe that is half-connected).

Workflow that works:
1. Tell the owner exactly where the probe tip and the ground clip go (see wiring templates below).
2. `wait` until the channel shows a swing, then `autoset` on that channel.
3. `describe --png`, look at the plot, re-adjust if a flag says so (clipping, low resolution,
   timebase too fast/slow for the feature of interest), then `describe` again.
4. For slow things (rail sag, thermal drift, intermittent faults): `log start`, come back later.

## Everything is recorded on the Pi — evaluate later with any criteria
The Pi records **every raw frame** it receives (1024 samples × 2 channels + settings + timestamp)
into `/home/pi/records/rec_*.bin`, hourly/64 MB segments, 40 GB ring (≈ 11 days at 20 fps), on
by default even with nobody watching. So a measurement never has to be repeated to re-judge it:
```bash
$P host/scopectl.py record status | start | stop          # stop = only record while someone watches
$P host/scopectl.py records list
$P host/scopectl.py replay --from=-10m --to=now --ch both --png /tmp/strip.png --csv /tmp/f.csv \
      --param glitch_ratio=0.3 --param hyst=0.1 --param low_res=16     # ANY criteria, post hoc
$P host/scopectl.py replay --from="2026-10-11 01:08" --to="01:09" --ch 2 --server   # same, computed on the Pi
$P host/scopectl.py records fetch --from=-1h out.json     # raw frames of a window, for anything else
```
Finding things in days of recording without reading it all — a per-frame metrics index
(amplitude, mean, AC RMS, frequency, clipped, glitches, duty, span per channel) is written next
to each segment by the Pi (and backfilled for older segments):
```bash
$P host/scopectl.py find --from=-4d --where "ch2_mean<4.5"            # rail dipped below 4.5 V: when, how often, how low
$P host/scopectl.py find --from=-1h --where "ch1_glitches>0,ch1_amp>2" # runt step pulses while the line was live
$P host/scopectl.py watch --where "ch2_mean<4.5" --for 10m            # live: "TRIPPED at 12:03:15" the moment it happens
```
`find` clusters matching frames into events (gap 2 s) and prints each event's extreme sample and
a ready-made `replay` command to look at it. Conditions: `chN_<amp|mean|rms|freq|clipped|glitches|duty|span> <op> value`,
comma-separated (all must hold).

Times: unix seconds, `-10m`/`-2h` ago, `HH:MM[:SS]` today, or `YYYY-MM-DD HH:MM[:SS]` (write
`--from=-10m` with the `=`). The analysis code is one module (`host/scope_analysis.py`) used by the
tool and by the Pi (`/api/analyze`), and all thresholds are `Params` fields — change them on the
command line, nothing else moves. Strip charts show amplitude / mean / frequency per frame over
time, which is how a sagging rail or an intermittent contact shows up.

## What the readings mean, and when NOT to trust them
- **Ranges:** 0–3 are uncalibrated on this unit (OpenScope has no gain for them) — counts only.
  Ranges 4 and 8/9 are "provisional" (±10 %). Ranges 5–7 are measured: CH1 within ~2 %,
  **CH2 reads ~6 % low** on ranges 6/7 (sweep 2026-10-10 against the Pi's 3.3 V reference).
- **Per-unit calibration lives on the Pi** (`/opt/scope/calib.json`, applied to every frame, the
  viewer, scopectl and the logger). Done 2026-10-11 for CH2 ranges 6 and 7 against the Pi's own
  reference (zero: GPIO18 driven low; gain: the 3.3 V square): zero −16.9 counts on both ranges
  (−0.65 V at range 6), gain ×1.062 / ×1.044. After that CH2 reads 3.30 V top, 0 V base. Other
  ranges and CH1 are uncorrected until `zero`/`gain` are run there: to zero, ask the owner to touch the
  tip to the circuit's ground (`zero` refuses if the input is not quiet); to set gain, apply a known
  amplitude (the Pi square is a good one for 1–7 V/div ranges).
- **Probe switch:** the scope only knows the probe factor from its own setting (`prb1/prb2` in
  `status`). If the owner's probe is physically at ×10 and the scope says ×1, every voltage is 10× low.
  Ask which position the switch is in; set the scope to match (`btn` menus, or ask him).
- **Timebase:** all codes within 0.2 % (verified). Records are 1024 samples; `describe` says the
  record length in seconds. Frequencies above ~fs/8 are refused rather than guessed.
- **Flags** in `describe`: clipped / no signal / low resolution / uncalibrated / frequency unstable
  (= intermittent contact or jitter) / ~60 Hz mains pickup (= a floating probe, missing ground clip).
- **Free-run vs triggered:** most records are free-run (the plot says so); the time axis is still
  exact, only the phase is random. Edge timing *within* a record is reliable; timing between two
  records is not.

## Safety limits (input)
- Scope input with the probe at **×1: keep it under 40 V**. At **×10: under 400 V peak**, and the
  probe's own rating applies (the supplied P6100-type probe is 600 V CAT II at ×10, 150 V at ×1).
  When in doubt use ×10 and tell the scope.
- Mains, spindle drives, VFD outputs: ×10, ground clip only to a true earth/ground, never to a
  floating node. If a measurement involves anything over 50 V, say so explicitly before asking
  the owner to connect it.
- The ground clips of both channels are the same net (and tied to the Pi and the Mac through
  USB). Two clips on two different potentials = a short through the scope.

## Wiring templates (copy into the chat)
- Logic / step / dir / enable / PWM (3.3–24 V): "Probe switch at ×1 (×10 if it might exceed 40 V).
  Ground clip on the controller's GND. Tip on the signal pin. Tell me when it's on."
- Power rail sag/noise: "×1, ground clip on GND right at the load, tip on the rail at the load.
  I'll log it while you run the job."
- Encoder / quadrature: "CH1 on A, CH2 on B, both ground clips on the encoder GND."
- Motor phase / anything switching: "×10, and clip only to earth ground."

## CNC-flavoured checks the tool can do today
- Step pulse rate/width/duty (`describe`: freq + duty; cursors in the viewer for pulse width).
- Direction setup/hold relative to step: CH1 step, CH2 dir, triggered view, cursors.
- Spindle PWM duty and frequency; enable/alarm line levels.
- Rail droop under load: `log start`, run the job, `log csv`; look at ch_mean over time.
- Noise coupling: `describe` on an idle line while the spindle/steppers run; amplitude + spectrum.

## Device behaviour worth knowing
- Auto power-off after 5 minutes without a button press or a shell command (firmware r12,
  `idle [off|<min>]`). The Pi's polling counts as activity, so while anyone is watching or the
  assistant is working, it stays on. The Pi stops polling 3 minutes after the last viewer, so an
  unattended scope will then doze. On USB power it falls into a dark sleep instead of a real
  power-off (VBUS holds the rail); any button or command wakes it.
- Buttons over the shell: `btn ch1|ch2` selects the channel AND cycles its coupling (DC→AC→GND);
  `btn menu` cycles modes (scope→meter→siggen→settings); `mode scope` returns.
- The viewer at http://scope-pi.local:8080 shows everything live; the assistant does not need it.
