#!/usr/bin/env python3
"""Per-unit vertical calibration of one channel against the Pi reference (GPIO18 on the probe tip,
Pi GND on the clip). For each range:
  zero: GPIO18 + GPIO12 driven LOW (0 V)  -> mean ADC count over N frames - 128   (sub-count, noise-dithered)
  gain: GPIO18 = 1 kHz square    -> top/base = means of the flat parts (edges excluded) over N frames,
                                    gain = REF_V / ((top - base) * firmware volts-per-count)
Writes both to the Pi's calib.json via POST /api/calib. Ranges where the square clips, or where the
swing is under --min-counts, get zero only.

    .venv/bin/python host/t_calibrate.py --ch 1 --ranges 4 5 6 7 8 9 --ref 3.194

REF_V: the Pi GPIO high level. 3.194 V = the owner's 2C53P meter on the 50 % PWM (1.597 V average,
2026-10-10); nominal would be 3.3 V. Restores the range, the PWM reference and the trigger mode.
"""
import argparse, base64, json, os, statistics, subprocess, sys, time, urllib.request

URL = os.environ.get("SCOPE_URL", "http://scope-pi.local:8080")
PI = os.environ.get("SCOPE_PI", "pi@scope-pi.local")


def api(path, body=None, timeout=20):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def cmd(line):
    r = api("/api/cmd", {"cmd": line})
    if not r.get("ok"):
        raise SystemExit("refused %s: %s" % (line, r))


OFFSET = {1: ("trig raw", 2748, 0.120), 2: ("trig2 raw", 2678, 0.123)}   # shell cmd, boot code (r15), counts/code


def raw_shell(line, wait=1.2):
    """Commands outside the web allow-list (offset DAC) go through the Pi's raw shell port."""
    import socket
    host = URL.split("//", 1)[1].split(":", 1)[0]
    s = socket.create_connection((host, 7001), timeout=5)
    s.settimeout(0.4)
    s.sendall(line.encode() + b"\r")
    t0, out = time.time(), b""
    while time.time() - t0 < wait:
        try:
            c = s.recv(4096)
            if not c:
                break
            out += c
        except socket.timeout:
            pass
    s.close()
    return out.decode(errors="replace")


def pi(sh):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", PI, sh], capture_output=True, text=True, timeout=30).stdout


def frames(n, settle=0.8):
    time.sleep(settle)
    out, last, t0 = [], None, time.time()
    while len(out) < n and time.time() - t0 < 20:
        f = api("/api/frame.json")
        if not f or f.get("seq") == last:
            time.sleep(0.03); continue
        last = f["seq"]
        out.append(f)
    return out


def flat_levels(raw):
    """Means of the high and low plateaus, excluding 3 samples around every transition."""
    mn, mx = min(raw), max(raw)
    mid = (mn + mx) / 2.0
    hi = [x > mid for x in raw]
    keep_hi, keep_lo = [], []
    n = len(raw)
    for i in range(n):
        if any(hi[j] != hi[i] for j in range(max(0, i - 3), min(n, i + 4))):
            continue
        (keep_hi if hi[i] else keep_lo).append(raw[i])
    return (statistics.fmean(keep_hi) if keep_hi else None, statistics.fmean(keep_lo) if keep_lo else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ch", type=int, required=True)
    ap.add_argument("--ranges", type=int, nargs="+", default=[4, 5, 6, 7, 8, 9])
    ap.add_argument("--ref", type=float, default=3.194, help="Pi GPIO high level in volts")
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--min-counts", type=float, default=8.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--shift-clipped", action="store_true",
                    help="for ranges where the square clips at the top, move the offset so its base sits just above the "
                         "ADC floor, measure the step (gain only depends on the step), then restore the boot offset")
    a = ap.parse_args()
    ch = a.ch
    f0 = api("/api/frame.json")
    h0 = f0["hdr"]
    rng0 = h0["rng%d" % ch]
    results = {}
    try:
        cmd("fpga scope trigmode auto")
        # ---- zero: pin driven low
        pi("sudo pinctrl set 18 op dl; sudo pinctrl set 12 op dl")     # both reference pins (CH2 may be on GPIO12, pin 32)
        for rng in a.ranges:
            cmd("fpga scope vdiv %d %d" % (ch, rng))
            fs = frames(a.frames)
            raws = [base64.b64decode(f["ch%d" % ch]) for f in fs]
            swing = max(max(r) - min(r) for r in raws)
            z = statistics.fmean(statistics.fmean(r) for r in raws) - 128.0
            results[rng] = {"zero": z, "zero_swing": swing}
            railed = min(min(r) for r in raws) < 50 and statistics.fmean(statistics.fmean(r) for r in raws) < 50
            if railed:
                results[rng]["zero"] = None
            print("CH%d range %d: zero %+.2f counts (swing %d)%s" % (ch, rng, z, swing,
                  "  RAILED at the ADC floor: centre the offset DAC first" if railed else ("  NOT QUIET" if swing > 12 else "")))
        # ---- gain: 1 kHz square
        pi("sudo pinctrl set 18 a5; sudo pinctrl set 12 a0; C=/sys/class/pwm/pwmchip0/pwm0; echo 0 | sudo tee $C/duty_cycle >/dev/null; "
           "echo 1000000 | sudo tee $C/period >/dev/null; echo 500000 | sudo tee $C/duty_cycle >/dev/null; echo 1 | sudo tee $C/enable >/dev/null")
        for rng in a.ranges:
            cmd("fpga scope vdiv %d %d" % (ch, rng))
            fs = frames(a.frames)
            amps, vpcs, clipped = [], [], False
            for f in fs:
                raw = base64.b64decode(f["ch%d" % ch])
                if min(raw) <= 0 or max(raw) >= 227:
                    clipped = True
                top, base = flat_levels(raw)
                if top is not None and base is not None:
                    amps.append(top - base)
                vpcs.append(f["hdr"]["vpc%d_nv" % ch] / 1e9)
            r = results[rng]
            vpc_fw = statistics.median(vpcs)
            if clipped and a.shift_clipped and vpc_fw > 0:
                cmd_s, boot, slope = OFFSET[ch]
                code = int(boot - (128 - 58) / slope)
                for _ in range(4):                         # walk the base to ~58 counts
                    raw_shell("%s %d" % (cmd_s, code))
                    fs2 = frames(4)
                    b = statistics.fmean(flat_levels(base64.b64decode(f["ch%d" % ch]))[1] or 0 for f in fs2)
                    if abs(b - 58) < 3:
                        break
                    code = int(code + (58 - b) / slope)
                fs = frames(a.frames)
                amps, clipped = [], False
                for f in fs:
                    raw = base64.b64decode(f["ch%d" % ch])
                    top, base = flat_levels(raw)
                    if max(raw) >= 227 or (base is not None and base < 50):
                        clipped = True
                    if top is not None and base is not None:
                        amps.append(top - base)
                raw_shell("%s %d" % (cmd_s, boot))
                print("CH%d range %d: offset shifted to code %d for the gain step (base %.1f counts)%s" % (ch, rng, code, b, " -- still clipped" if clipped else ""))
            bases = [flat_levels(base64.b64decode(f["ch%d" % ch]))[1] for f in fs]
            if any(b is not None and b < 50 for b in bases):
                clipped = True        # this ADC floors near 44 counts: a base there is railed, not measured
            if clipped:
                r["gain"] = None; r["note"] = "clipped/railed"
            elif not amps or statistics.median(amps) < a.min_counts:
                r["gain"] = None; r["note"] = "swing %.1f counts < %g" % (statistics.median(amps) if amps else 0, a.min_counts)
            elif vpc_fw <= 0:
                r["gain"] = None; r["note"] = "uncalibrated range in firmware"
            else:
                amp = statistics.fmean(amps)
                r["gain"] = a.ref / (amp * vpc_fw)
                r["amp_counts"] = amp
                r["spread"] = (max(amps) - min(amps)) / amp
                r["note"] = "firmware %.4g V/count -> measured %.3f V before correction" % (vpc_fw, amp * vpc_fw)
            print("CH%d range %d: %s" % (ch, rng, ("gain x%.4f (%.1f counts, spread %.1f%%; %s)" % (r["gain"], r["amp_counts"], r["spread"] * 100, r["note"])) if r.get("gain") else "no gain (%s)" % r["note"]))
        if not a.dry_run:
            for rng, r in results.items():
                body = {"ch": ch, "rng": rng, "clear": True}
                if r.get("zero") is not None:
                    body["zero"] = r["zero"]
                if r.get("gain"):
                    body["gain"] = r["gain"]
                api("/api/calib", body)
            print("stored on the Pi:", json.dumps(api("/api/calib")))
    finally:
        pi("sudo pinctrl set 18 a5; sudo systemctl restart pwm-ref")
        cmd("fpga scope vdiv %d %d" % (ch, rng0))
        print("restored CH%d range %d and the 1 kHz reference" % (ch, rng0))


if __name__ == "__main__":
    main()
