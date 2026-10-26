# Pi Zero 2 W inside the 2C53T — build guide

Goal: one sealed unit. The Pi runs off the scope's battery, turns on and off with the scope,
talks to it over the scope's own USB data lines, and still lets a computer use the USB-C port
when a switch is flipped. The software side is already done (see "What the software already
does" at the end); this file is the hardware.

**Before cutting or soldering anything: open the case, take clear photos of both sides of the
main board (especially around the USB-C port, the battery connector and the SWD header) and
send them. The exact solder points for D+/D−, 3V3 and the battery are confirmed from those
photos, not guessed.**


## 0. Current plan (2026-10-14): Pi OUTSIDE the case on its OWN supply; a normal USB cable charges the scope

Decided after the battery-fed attempt drained the scope's cell to 2.98 V and cost a day of CNC work (below).
- **Pi power:** its own micro-USB supply in "PWR IN" (official 5 V 2.5 A, or the machine's 5 V, or a power bank).
- **Data + charging:** a normal USB data cable from the Pi's "USB" port to the scope, **5 V wire intact**: the
  Pi's port charges the scope continuously, so the scope runs indefinitely at the CNC. Verify with
  `scopectl battery` (state must read "charging" or "steady", never "discharging", while connected).
- **POWER on USB:** firmware r18 dark-sleeps (USB holds the rail up) and only a button wakes it; the Pi sees `slp=2`,
  backs off to 1 Hz and pauses recording.
- **Nothing soldered inside the scope is needed.** Remove the battery leads at the battery end (not just at the Pi) and
  tape the 3V3 trigger wire if it stays.
- **Pi mounting:** outside the case, antenna end toward the router.

### Tried and abandoned (keep for reference)
- **Pi fed straight from the scope's Li-ion cell (2026-10-13/14).** It runs: the Zero's own 3.3 V regulator copes down to
  about 3.4 V. But with no switch it drew 0.3–0.5 A around the clock, scope on or off. Its USB port only outputs the cell
  voltage, so nothing charged the scope. The cell went 3.81 V → 2.98 V in about a day, and the scope's battery-critical
  shutdown (30 s below about 3.3 V) kept switching it off. The Zero 2 W has no undervoltage detector, so `get_throttled=0x0`
  proves nothing there.
- **Fixes considered for battery-feeding:**
  - Pololu S9V11F5: EN is not one of the three holes. Its pad wasn't identified from the drawing.
  - Pololu S7V8F5: SHDN is a through-hole, but it needs <0.4 V against a 100k pull-up, so use a ~5k pull-down.
  - A diode-OR hold (scope 3V3 / Pi GPIO27 → EN, `gpio-shutdown` on GPIO17, `gpio-poweroff` on GPIO27).
  - A latching switch: Pololu Mini Pushbutton Power Switch **LV #2808**. The SV version needs ≥4.5 V. One #2808 was
    ordered and is now a spare.
  - All of these fix the drain while the scope is off, but not the runtime while working, nor charging. Cheap
    "MOSFET trigger" modules switch ground; USB GND then back-feeds, so they never fully cut power.
- **Internal D+/D− tap.** On the owner's board (2C53T-V1.4_20250507) the USB-C receptacle is the 16-pin USB 2.0
  type: pads 6+7 (the middle two) are D+/D−, and 2/11 are VBUS. Mapped in `docs/internal_build/own_board_usbc.jpg`.
  It was dropped for fine-pitch soldering. Battery +/−, 3V3 and GND are confirmed on the owner's board
  (`own_board_taps.jpg`).

---

## 1. Parts

| Part | Why | Example |
|---|---|---|
| 5 V buck-boost regulator **with EN pin and true shutdown** | battery (3.0–4.2 V) → 5 V for the Pi, switched by the scope | [Pololu S9V11F5](https://www.pololu.com/product/2836) (2–16 V in, EN, 7.6 × 11.4 mm, ~$7) |
| Resettable fuse (PTC), **1.35 A hold / ~2.7 A trip**, ≥6 V | a short on the Pi side (or on the wires leaving the case) must not reach the Li-ion cell | Bourns MF-R135 or Littelfuse RXEF135 (radial, easy inline). Sizing: Pi Zero 2 W peak ~3 W → ~1.07 A from a 3.3 V cell at ~85 % efficiency; a 1.1 A-hold part derates to ~0.94 A at 50 °C and could nuisance-trip, so 1.35 A. Power wires 26 AWG |
| DPDT micro slide switch | moves the USB data lines between "Pi" and "external" | any SMD/through-hole DPDT, 2 positions |
| Resistors 15 kΩ + 22 kΩ | VBUS sense divider (5 V → ~3 V for a Pi GPIO) | 1/8 W or 0603 |
| Resistor 10 kΩ | in series with the regulator's EN wire | |
| Thin insulated wire | 30 AWG silicone or Kynar wire-wrap wire | |
| Kapton tape, thin double-sided foam tape, heat-shrink | insulation and mounting | |

Not used: the scope board's own "5V" pad (its rails are sized for the scope), the Pi's micro-USB
power socket (the Pi is fed on its 5 V pin instead), the scope's VBUS for the Pi (see 3).

## 2. Connections

Pictures: [scope-side taps](docs/internal_build/scope_taps.jpg) (battery +/−, 3V3, GND marked on
OpenScope's photo of a V1.4 board; USB D+/D−/VBUS still to be located from a photo of this unit's
USB-C area) and [Pi pins](docs/internal_build/pi_pins.png) (pins 2, 6, 11 and the USB data port,
drawn in the bench orientation: SD card up, header on the right). Regenerate with
`tools/make_build_images.py`.

```
 scope battery +  ──[ PTC fuse ]──► S9V11F5 VIN          S9V11F5 VOUT ──► Pi 5V  (header pin 2)
 scope battery −  ─────────────────► S9V11F5 GND ─────────────────────► Pi GND  (header pin 6)
 scope 3V3 (SWD header) ──[10k]──► S9V11F5 EN           (scope on ⇒ Pi on, scope off ⇒ Pi off)

 scope USB D+ ──┐                                          ┌── Pi USB D+ ┐  Pi micro-USB "USB" port
 scope USB D− ──┤  DPDT switch: "Pi" = connected,          ├── Pi USB D− ┘  (D+/D− test pads PP22/PP23
                │               "external" = Pi isolated   │                 on the Pi's underside, or
                └──────────────────────────────────────────┘                 a cut-down OTG cable)
 scope GND ─────────────────────────────────────────────────── Pi GND (same as above)

 scope USB-C VBUS ──[15k]──┬──► Pi GPIO17 (header pin 11)     VBUS sense: "is a charger plugged in?"
                           └──[22k]── GND
```

Rules:
- **Switching:** the battery only feeds the regulator; the scope's 3V3 on EN switches it. Scope on ⇒ Pi on,
  scope off (on battery) ⇒ Pi off. On USB the scope's rails stay up even when "off", so the Pi stays on
  while charging. The owner OK'd that (2026-10-13): it keeps recording and stays reachable. Before
  soldering, confirm with a meter that the 3V3 pad reads 0 V with the scope off on battery.
- **Never connect the Pi's 5 V to the scope's VBUS.** The scope would then "charge" its own battery
  from its own battery through two converters.
- The switch only connects or disconnects the Pi's data pair. The scope's USB-C connector stays
  wired to the scope as it is now; no PCB traces are cut. In "external", the Pi hangs a few cm of
  open wire on the bus, which full-speed USB tolerates.
- **Flip to "external" before plugging a computer into the USB-C port.** Two hosts on one bus do
  not work.
- **Charging while the switch is on "Pi":** many wall chargers short D+ to D− to announce
  themselves, which breaks the Pi link while plugged in. Use a charge-only USB-C cable or a USB
  "data blocker" between such a charger and the scope. A computer's port or a "dumb" 5 V supply is
  fine.

## 3. Checks before closing the case (in this order)

1. **Regulator really switches off.** Battery side connected, EN wire to GND: VOUT must read ≈ 0 V
   (some boost modules pass the battery straight through when disabled — that would half-power
   the Pi forever). EN to 3V3 (scope on): VOUT 5.0 V.
2. **Pi boots from the scope.** Scope on → Pi green LED activity within a few seconds, on WiFi in
   ~30–40 s (`scope-pi.local`). Scope off → Pi dark.
3. **USB without VBUS.** Switch on "Pi", no cable in the USB-C port: `scopectl health` must show
   the link and frames. The scope's firmware does not need VBUS to talk over USB (checked in the
   source); the factory **upgrade mode** has not been tested without VBUS — check it too: MENU held
   + pinhole reset, then on the Pi `python3 iap_flash.py status` must find the IAP drive. If it
   does not, the fallback is the switch on "external" and a computer.
4. **Switch on "external"** with the Mac on the USB-C port: the Mac sees the scope as before.
5. **VBUS sense:** plug a charger in and out; `scopectl health` must flip between "scope on USB
   power" and "ON BATTERY".
6. **Power-off notice:** hold POWER until "Goodbye": the Pi log (`journalctl -u scope-server`)
   shows `recorder quiesced: scope power-off notice` within the 3 s before power drops.

## 4. Mounting (agreed 2026-10-10)

- Pull the battery before any Dremel work; keep screw bosses, latches and the outer wall intact;
  blow out all dust.
- Pi at the BNC end or beside the battery, **away from the multimeter jacks, fuses and relay** (up
  to 300 V lives there). Kapton under the Pi, foam tape to hold it, nothing conductive touching.
- WiFi antenna end of the Pi (the end away from the SD card) facing plastic, not the battery or a
  shield.
- The SD card stays reachable only by opening the case — the software is all remote, so that is
  acceptable; take a full image of the card first (`dd` from the Mac) as a spare.
- Test-fit with tape before soldering anything.

## 5. Power budget (estimate)

Pi Zero 2 W while streaming and recording: ~0.6–1.2 W at 5 V → ~0.8–1.4 W from the cell through
the regulator. Read the battery's mAh off its label when the case is open and the runtime estimate
can be made exact. Expect noticeably shorter battery life than the scope alone (the owner accepted
this). To trim it: CPU clock capped (`arm_freq=700` in `config.txt`), HDMI/audio off, activity LED
off — done in software when the build is finished.

## 6. What the software already does (firmware r14 + current Pi server)

- **Power-off notice:** every power-off path in the firmware (POWER button, 5-min auto-off,
  battery-critical shutdown) raises `off=1` in the record header and waits 3 s before releasing the
  power hold. The Pi sees it within ~60 ms, fsyncs and closes the current recording segment and its
  index, and syncs the filesystem. If power does not drop (USB keeps the rail up), recording resumes
  15 s later. Read-only root is therefore not used: the recordings must persist, and the notice
  makes the cut safe. Remaining unprotected cases: a pinhole reset or a battery disconnect while
  on battery (the ext4 journal limits damage to the last seconds).
- **Battery-aware behaviour:** start the server with `--pi-power internal --vbus-gpio 17`
  (edit `/etc/systemd/system/scope-server.service`). Then:
  on USB power → record always (as now); on battery → record while someone is watching or a log /
  `record start` is active, stop polling 3 min after the last viewer, which lets the scope's 5-min
  auto-off turn everything off. The scope's own "charging" flag cannot be used for this — it is a
  battery-voltage threshold and reads "no" on USB once the cell is full — hence the VBUS wire.
- `scopectl health` shows the power situation; the shell has `power` (battery mV/%, auto-off
  state) and `idle [off|<min>]`.
