#!/usr/bin/env python3
"""Characterise timebase codes of the 2C53T against the Pi's hardware PWM (its own crystal).

For each code, drive several known frequencies into the probed channel, capture frames through
the viewer API, and measure the period in SAMPLES directly from the raw record (seam-tolerant
edge estimator, scope_analysis.period_samples). rate = f * period_samples. A code is accepted
when at least two frequencies agree on the rate within `--agree` (default 1 %) and the per-frame
spread is small; otherwise it is reported as inconsistent (OpenScope's "incoherent" band).

    .venv/bin/python host/t_tbsweep.py --ch 2 --codes 0x0D 0x0C 0x0B 0x0A 0x09 0x08
    (restores the timebase and the 1 kHz reference afterwards)

Requires: the Pi reference on the channel (GPIO18 -> probe tip, GND -> clip), ssh access to the
Pi (pwm sysfs), the viewer at SCOPE_URL.
"""
import argparse, base64, json, os, statistics, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_analysis as SA

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
        raise SystemExit("refused: %s -> %s" % (line, r))
    return r.get("out", "")


def pwm(freq_hz):
    period = int(round(1e9 / freq_hz))
    duty = period // 2
    sh = ("C=/sys/class/pwm/pwmchip0/pwm0; echo 0 | sudo tee $C/duty_cycle >/dev/null; echo %d | sudo tee $C/period >/dev/null; "
          "echo %d | sudo tee $C/duty_cycle >/dev/null; cat $C/period" % (period, duty))
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", PI, sh], capture_output=True, text=True, timeout=30).stdout.strip()
    actual = 1e9 / int(out) if out.isdigit() and int(out) > 0 else None
    return actual


def frames(n, settle=0.5):
    time.sleep(settle)
    out, last, t0 = [], None, time.time()
    while len(out) < n and time.time() - t0 < 15:
        f = api("/api/frame.json")
        if not f or f.get("seq") == last:
            time.sleep(0.03); continue
        last = f["seq"]
        out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ch", type=int, default=2)
    ap.add_argument("--codes", nargs="+", default=["0x0D", "0x0C", "0x0B", "0x0A", "0x09", "0x08"])
    ap.add_argument("--freqs", nargs="+", type=float, default=[2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6])
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--agree", type=float, default=0.01)
    ap.add_argument("--json")
    a = ap.parse_args()
    info = api("/api/info")
    h0 = api("/api/frame.json")["hdr"]
    results = {}
    try:
        for code_s in a.codes:
            code = int(code_s, 16)
            cmd("fpga scope timebase 0x%02X" % code)
            print("\n== code 0x%02X" % code)
            ests = []
            for f in a.freqs:
                actual = pwm(f)
                if not actual:
                    print("  pwm %.4g Hz: not settable" % f); continue
                fs = frames(a.frames, settle=0.6)
                pers, spans = [], []
                for fr in fs:
                    raw = base64.b64decode(fr["ch%d" % a.ch])
                    spans.append(max(raw) - min(raw))
                    rises, _, _ = SA.edges(raw, 0.15, 6)
                    p = SA.period_samples(rises)
                    if p: pers.append(p)
                if len(pers) < max(2, a.frames // 2) or max(spans) < 12:
                    print("  f %9.4g Hz: no usable period (%d/%d frames, swing %d counts)" % (actual, len(pers), len(fs), max(spans) if spans else 0)); continue
                pm = statistics.median(pers)
                spread = (max(pers) - min(pers)) / pm
                rate = actual * pm
                ok = 8 <= pm <= 400 and spread < 0.02
                print("  f %9.4g Hz: period %7.2f samples (spread %.2f%%, %d frames) -> %10.1f S/s %s" % (actual, pm, spread * 100, len(pers), rate, "" if ok else "(rejected)"))
                if ok:
                    ests.append({"f": actual, "period": pm, "spread": spread, "rate": rate})
            verdict = {"code": code, "estimates": ests, "rate": None, "status": "no data"}
            if ests:
                rates = [e["rate"] for e in ests]
                med = statistics.median(rates)
                agree = [r for r in rates if abs(r / med - 1) <= a.agree]
                if len(agree) >= 2:
                    verdict["rate"] = sum(agree) / len(agree)
                    verdict["status"] = "measured (%d of %d frequencies agree within %.1f%%)" % (len(agree), len(rates), a.agree * 100)
                elif len(rates) == 1:
                    verdict["rate"] = rates[0]; verdict["status"] = "single frequency only (provisional)"
                else:
                    verdict["status"] = "INCONSISTENT: rates %s" % ", ".join("%.0f" % r for r in rates)
            print("  -> 0x%02X: %s%s" % (code, verdict["status"], (" = %.1f S/s (%s)" % (verdict["rate"], SA.fmt_rate(verdict["rate"]))) if verdict["rate"] else ""))
            results["0x%02X" % code] = verdict
    finally:
        cmd("fpga scope timebase 0x%02X" % h0["tb"])
        pwm(1000.0)
        print("\nrestored timebase 0x%02X and the 1 kHz reference" % h0["tb"])
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(results, fh, indent=1)
        print("saved", a.json)


if __name__ == "__main__":
    main()
