#!/usr/bin/env python3
"""Calibration check for the 2C53T through the Pi viewer's HTTP API.

Feed the scope a known signal (e.g. the 2C53P's generator: sine, known frequency and
amplitude at the BNC), then run:

    .venv/bin/python host/t_calcheck.py --freq 1000 --vpp 0.33 [--ch 2] [--probe 1]
        [--url http://scope-pi.local:8080]

It sweeps every timebase where the expected frequency gives 2..100 cycles per record
and every vertical range where the expected Vpp fits in the screen, averages a few
frames at each setting and prints the measured vs expected error. The scope is left
on the last setting of each sweep (use the viewer to put it back).

Horizontal error = (measured period / expected period - 1): a +1.5 % error means the
scope's sample-rate table for that code is 1.5 % too high (the timebase labels read
short). Vertical error = measured Vpp / expected Vpp - 1 for that range's mV/count.
"""
import argparse, json, math, statistics, time, urllib.request


def api(url, path, body=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def frames(url, n, settle=0.6):
    time.sleep(settle)
    out, last = [], -1
    while len(out) < n:
        f = api(url, "/api/frame.json")
        if f.get("seq") != last:
            out.append(f); last = f["seq"]
        else:
            time.sleep(0.05)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://scope-pi.local:8080")
    ap.add_argument("--freq", type=float, required=True, help="generator frequency, Hz")
    ap.add_argument("--vpp", type=float, required=True, help="generator Vpp at the BNC (tip volts)")
    ap.add_argument("--ch", type=int, default=2, choices=(1, 2))
    ap.add_argument("--probe", type=int, default=1, help="probe factor the scope is set to (1 or 10)")
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--wave", choices=("square", "sine"), default="sine",
                    help="square: amplitude = top-base levels; sine: amplitude = 2*sqrt(2)*AC RMS")
    ap.add_argument("--skip-h", action="store_true"); ap.add_argument("--skip-v", action="store_true")
    a = ap.parse_args()
    info = api(a.url, "/api/info")
    ch = "ch%d" % a.ch
    cpd = info["counts_per_div"]
    h0 = api(a.url, "/api/frame.json")["hdr"]          # restore these settings at the end
    try:
        sweep(a, info, ch, cpd)
    finally:
        api(a.url, "/api/cmd", {"cmd": "fpga scope timebase 0x%02X" % h0["tb"]})
        api(a.url, "/api/cmd", {"cmd": "fpga scope vdiv %d %d" % (a.ch, h0["rng%d" % a.ch])})
        print("\nrestored timebase 0x%02X, CH%d range %d" % (h0["tb"], a.ch, h0["rng%d" % a.ch]))


def sweep(a, info, ch, cpd):

    if not a.skip_h:
        print("\nHorizontal (freq %.6g Hz expected)" % a.freq)
        print("%-6s %-12s %-10s %-12s %-10s %s" % ("code", "s/div", "tier", "meas Hz", "err %", "n"))
        for t in sorted(info["timebases"], key=lambda t: -t["rate"]):
            cycles = a.freq * 1024 / t["rate"]
            if not (2 <= cycles <= 100):
                continue
            api(a.url, "/api/cmd", {"cmd": "fpga scope timebase 0x%02X" % t["code"]})
            fs = [f["meas"][ch].get("freq") for f in frames(a.url, a.frames)]
            fs = [f for f in fs if f]
            if not fs:
                print("%-6s %-12s %-10s no edges" % ("0x%02X" % t["code"], "%.3g" % t["s_per_div"], t["tier"])); continue
            m = statistics.median(fs)
            print("%-6s %-12s %-10s %-12.5g %+-10.2f %d" % ("0x%02X" % t["code"], "%.3g" % t["s_per_div"], t["tier"], m, (m / a.freq - 1) * 100, len(fs)))

    if not a.skip_v:
        print("\nVertical CH%d (Vpp %.4g expected, probe x%d)" % (a.ch, a.vpp, a.probe))
        print("%-5s %-12s %-12s %-12s %-10s %s" % ("rng", "V/div", "tier", "meas Vpp", "err %", "clipped"))
        mvpc = info["mv_per_count"][str(a.ch)] if str(a.ch) in info["mv_per_count"] else info["mv_per_count"][a.ch - 1]
        for rng in range(9, -1, -1):
            mv = mvpc[rng]
            if not mv:
                continue
            vdiv = mv / 1000 * cpd * a.probe
            if a.vpp > vdiv * 8 * 0.98 or a.vpp < vdiv * 0.3:
                continue
            api(a.url, "/api/cmd", {"cmd": "fpga scope vdiv %d %d" % (a.ch, rng)})
            fr = frames(a.url, a.frames)
            key = "vamp" if a.wave == "square" else "vrms_ac"
            vs = [f["meas"][ch][key] * (1 if a.wave == "square" else 2 * math.sqrt(2))
                  for f in fr if f["meas"][ch].get("unit") == "V"]
            clip = any(f["meas"][ch].get("clipped") for f in fr)
            if not vs:
                print("%-5d %-12s %-12s uncal" % (rng, "%.3g" % vdiv, info["range_tier"][rng])); continue
            m = statistics.median(vs)
            print("%-5d %-12s %-12s %-12.4g %+-10.2f %s" % (rng, "%.3g" % vdiv, info["range_tier"][rng], m, (m / a.vpp - 1) * 100, "YES" if clip else ""))


if __name__ == "__main__":
    main()
