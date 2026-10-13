# CNC bring-up with the scope (Mesa card + LinuxCNC)

The owner wires one thing at a time; the assistant runs the check and says PASS / FAIL / MARGINAL with
numbers. Every check is `scopectl cnc <check>` (see `--help`); each exits 0 on PASS.

```
P=".venv/bin/python host/scopectl.py"
```

MARGINAL means the result is within the scope's own resolution of the limit. The scope cannot
decide that case; take it as "re-measure at a finer range/timebase, or add margin in the config".

## Safety first (3-phase machine)
- Probe **only the low-voltage side**: card I/O, drive *inputs*, 5 V / 24 V supplies, encoder and switch
  wiring. **Never** the VFD output, the motor phases, the drives' DC bus or anything on the 3-phase side.
  Those need a differential probe; the ground clip is not safe there.
- The scope's ground clip is the same net on both channels and is tied to the Pi. Clip only to the
  controller's logic/field GND (the card's GND or the 24 V supply's 0 V). Two clips on two different
  grounds = a short through the scope.
- Probe at ×1 below 40 V, else ×10 (and `scopectl probe --ch N --x10`). 24 V field I/O is fine at ×1.
- Machine power on, spindle off, E-stop in reach for anything that makes an axis move.

## Order of work
| # | What | Wiring (tell the owner) | Command |
|---|------|---------------------|---------|
| 1 | 24 V field supply, idle and with drives enabled | CH1 tip on +24 V at the card's field-power terminal, clip on its 0 V | `$P cnc rail --nominal 24 --seconds 10` |
| 2 | 5 V logic supply of the card (if it has one) | CH1 tip on +5 V, clip on GND | `$P cnc rail --nominal 5` |
| 3 | Enable / watchdog / E-stop output levels | CH1 tip on the output, clip on field 0 V | `$P cnc levels --family 24v` (or `--vil/--vih` from the receiver's datasheet) |
| 4 | Step/dir for each axis **at the drive's input terminals** while jogging back and forth | CH1 on STEP+, CH2 on DIR+, clips on the drive's input common (for differential inputs: STEP− / DIR−) | `$P cnc step --min-pulse … --min-setup … --max-rate …` (drive datasheet numbers) |
| 5 | Spindle encoder / MPG | CH1 on A, CH2 on B, clips on encoder GND; turn it by hand, then run the spindle slowly | `$P cnc encoder --lines <PPR>` |
| 6 | Limit/home switches: bounce vs the input filter | CH1 on the input pin, clip on field 0 V; the owner presses the switch when told | `$P cnc bounce --debounce <filter time>` (line idling low: add `--vhigh 24`) |
| 7 | Spindle command (0-10 V or PWM) at several commanded speeds | CH1 on the output, clip on its common | `$P describe --ch 1` per speed, or `$P log start` / `log csv` while stepping the speed |
| 8 | Noise on the I/O with the VFD running | CH1 on an idle input; spindle on | `$P cnc levels --family 24v` and `$P describe` (spectrum). Mark the moments with `$P mark "spindle on"` |

## Step/dir → LinuxCNC settings
`cnc step` measures what the card actually puts out. The hostmot2 stepgen settings, all in
nanoseconds, are `hm2_<card>.0.stepgen.NN.steplen`, `.stepspace`, `.dirsetup` and `.dirhold`
(usually set in the INI as `STEPLEN`, `STEPSPACE`, `DIRSETUP`, `DIRHOLD`). The measured pulse, space,
setup and hold must each be at least the drive's minimum, with margin. If a check FAILs, raise
the matching setting.
- `--active falling`: the drive counts on the falling edge, or the card's step output is inverted (`step_type`, invert pins).
- Setup and hold are only measured when the record contains a direction change. Jog back and forth in short moves while the check runs.
- The default limits are those of a typical microstepping driver (2.5 µs pulse and space, 5 µs setup and hold). **The drive's datasheet wins**, so pass its numbers.
- `--steps-per-mm` turns the step rate into feed (mm/min). Use it to check the scale while jogging at a known feed.

## Notes on trust
- Records are 1024 samples. Step pulses are measured at 5 MS/s (0.2 µs resolution) when they fit;
  slower pulses automatically use a slower timebase. Resolution is printed with every result.
- Short pulses read 2-3 % high in level (probe settling). Logic thresholds have far more margin than that.
- Ripple: on ranges 6-9 the scope's own noise is up to ~7 ADC steps peak-to-peak (`NOISE_PP_COUNTS` in
  `host/cnc_checks.py`). Ripple below that is reported as "at the scope's noise floor"; on a 24 V
  range that floor is ~1 V, so `rail` verifies level and sag well but not millivolt ripple. For
  small ripple use `avg` or a finer range with an offset (not automated).
- `bounce` triggers on CH1 only (the trigger source is CH1, and the shell cannot change it). The
  triggered record is un-rotated in software (`unrotate_triggered`: the trigger is the first edge, 512
  samples into the record). The default window is 20 ms. If the result says the last transition is near
  the end, rerun with `--window 50ms`.
- The logic analyzer, once it arrives, does all axes at once over seconds (`scopectl la capture` then
  `la stats --step D0 --dir D1 --steps-per-mm …`). The scope does analog levels, edges and noise.

## Verified (2026-10-12, Pi reference on CH1)
- `cnc step`: a 20 kHz, 5 µs pulse train measured 4.98 µs and 20.01 kHz, PASS against the defaults.
- `cnc levels --family 3v3`: highs 3.27 V and lows −20 mV. The timebase switched to 5 MS/s by itself to resolve the 5 µs plateaus.
- `cnc rail --nominal 3.3`: level PASS. Ripple MARGINAL, correctly: a 66 mV limit is below the range-6 noise floor.
- `cnc bounce`: a synthetic 6-pulse bounce from the Pi's GPIO (direct register writes) read 13 transitions over 3.95 ms and settled high, matching the ~3.8 ms that was sent.
- `cnc encoder`: CH1 on GPIO18, CH2 on GPIO12, driven by a real-time quadrature generator on the Pi (250 Hz, 90°): 20/20 frames PASS, phase 90.0°, duty cycle 50/50, direction A leads B, 15.0 rpm at 1000 lines. Earlier it FAILed while CH1's hook had lost contact (A stuck high or low for 40 ms at a time), which shows it catches a dropped encoder channel.
- Tests: `tests/test_cnc_checks.py` (14 tests); whole suite 28/28.
