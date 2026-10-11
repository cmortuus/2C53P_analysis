# Operating the 2C53T remotely — guide for the assistant (and anyone driving it blind)

Purpose (the owner, 2026-10-10): "I want to be able to hook up wires and have you do the rest" —
e.g. debugging the CNC without snapping pictures of the screen or a back-and-forth about settings.
So the assistant sets up the scope, takes the measurements, judges whether they can be trusted,
and explains in plain language. The owner only connects probes.

## The one tool: `host/scopectl.py` (runs on the Mac, talks to the Pi's API)
```bash
P=.venv/bin/python
$P host/scopectl.py status                       # link, settings, both channels at a glance
$P host/scopectl.py wait --ch any --timeout 300  # block until a probe sees something (ask the owner to hook up, then run this)
$P host/scopectl.py autoset --ch 2 --png /tmp/a.png   # finest non-clipping range, ~10 cycles, mid-level trigger
$P host/scopectl.py describe --png /tmp/d.png    # both channels: shape, freq, levels, flags + a plot to LOOK at
$P host/scopectl.py describe --ch 1 --json       # machine-readable
$P host/scopectl.py set "fpga scope timebase 0x10"   # any allow-listed shell command
$P host/scopectl.py screen /tmp/lcd.png          # the device's own LCD
$P host/scopectl.py log start | stop | csv out.csv   # long recordings (1 row per frame, hours)
```
Always read the plot (Read tool on the PNG). The text summary is good, the plot catches what
the summary cannot (glitches, ringing, asymmetry, a probe that is half-connected).

Workflow that works:
1. Tell the owner exactly where the probe tip and the ground clip go (see wiring templates below).
2. `wait` until the channel shows a swing, then `autoset` on that channel.
3. `describe --png`, look at the plot, re-adjust if a flag says so (clipping, low resolution,
   timebase too fast/slow for the feature of interest), then `describe` again.
4. For slow things (rail sag, thermal drift, intermittent faults): `log start`, come back later.

## What the readings mean, and when NOT to trust them
- **Ranges:** 0–3 are uncalibrated on this unit (OpenScope has no gain for them) — counts only.
  Ranges 4 and 8/9 are "provisional" (±10 %). Ranges 5–7 are measured: CH1 within ~2 %,
  **CH2 reads ~6 % low** on ranges 6/7 (sweep 2026-10-10 against the Pi's 3.3 V reference).
- **Vertical offset:** the frontend is centred by the firmware at boot only; CH2 has shown a −0.6 V
  offset at range 6 (a 0..3.3 V square read −0.65..2.46 V). Amplitudes are right; absolute DC
  levels can be off by that much. To pin DC: ask the owner to touch the tip to the circuit's ground
  and `describe` — that reading is the zero error, subtract it.
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
