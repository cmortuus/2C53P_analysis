#!/usr/bin/env python3
"""scopectl — operate the 2C53T through the Pi viewer's HTTP API, for an operator who
cannot see the screen (an AI assistant, a script, a remote human).

    .venv/bin/python host/scopectl.py status
    .venv/bin/python host/scopectl.py wait [--ch 1|2|any] [--timeout 120]   # until a signal appears
    .venv/bin/python host/scopectl.py autoset [--ch 1|2]                     # range, timebase, trigger
    .venv/bin/python host/scopectl.py describe [--ch 1|2|both] [--png out.png] [--frames 5] [--json]
    .venv/bin/python host/scopectl.py set "fpga scope timebase 0x0F"         # any allow-listed shell cmd
    .venv/bin/python host/scopectl.py screen out.png                         # the device's own LCD
    .venv/bin/python host/scopectl.py log start|stop|status|csv out.csv

Everything goes through http://scope-pi.local:8080 (override with --url or SCOPE_URL).
`describe` prints a plain-language summary plus flags (clipped, no signal, low resolution,
uncalibrated range, intermittent, mains pickup) and can render a PNG of the record with
real volt/time axes (matplotlib on this machine, nothing needed on the Pi).
"""
import argparse, base64, json, math, os, statistics, sys, time, urllib.request

URL = os.environ.get("SCOPE_URL", "http://scope-pi.local:8080")
CPL = ["DC", "AC", "GND"]
TMODE = ["auto", "normal", "single"]


# ------------------------------------------------------------------ API helpers
def api(path, body=None, timeout=20):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data) if r.headers.get("Content-Type", "").startswith("application/json") else data


def cmd(line):
    r = api("/api/cmd", {"cmd": line})
    if not r.get("ok"):
        raise SystemExit("scope refused %r: %s" % (line, r.get("out") or r.get("error")))
    return r.get("out", "")


def frames(n=3, settle=0.4):
    """n distinct frames (after `settle` s), decoded."""
    time.sleep(settle)
    out, last = [], None
    deadline = time.time() + 10
    while len(out) < n and time.time() < deadline:
        f = api("/api/frame.json")
        if not f or f.get("seq") == last:
            time.sleep(0.05)
            continue
        last = f["seq"]
        f["b1"] = base64.b64decode(f["ch1"])
        f["b2"] = base64.b64decode(f["ch2"])
        out.append(f)
    if not out:
        raise SystemExit("no frames from the viewer (is the scope connected? `scopectl status`)")
    return out


def decode(f):
    if f and "b1" not in f:
        f["b1"] = base64.b64decode(f["ch1"]); f["b2"] = base64.b64decode(f["ch2"])
    return f


def fmt_rate(r):
    return ("%.4g MS/s" % (r / 1e6)) if r >= 1e6 else ("%.4g kS/s" % (r / 1e3)) if r >= 1e3 else ("%.4g S/s" % r)


def ch_fields(f, ch):
    decode(f)
    h = f["hdr"]
    return {"raw": f["b%d" % ch], "vpc": f["vpc%d" % ch], "rng": h["rng%d" % ch], "en": h["en%d" % ch],
            "cpl": CPL[h["cpl%d" % ch]], "probe": 10 if h["prb%d" % ch] else 1, "meas": f["meas"]["ch%d" % ch]}


# ------------------------------------------------------------------ analysis
def analyze(fs, ch, info):
    """Combine several frames of one channel into a description dict."""
    recs = [ch_fields(f, ch) for f in fs]
    c = recs[-1]
    rate = fs[-1]["rate"]
    vpc = c["vpc"]
    cal = vpc > 0
    unit = "V" if cal else "cnt"
    k = vpc if cal else 1.0

    amps = [r["meas"]["vamp"] for r in recs]
    freqs = [r["meas"]["freq"] for r in recs if r["meas"].get("freq")]
    m = c["meas"]
    raw = c["raw"]
    mn, mx = min(raw), max(raw)
    span_counts = mx - mn
    amp = statistics.median(amps)
    noise_counts = statistics.median(sorted(abs(x - y) for x, y in zip(raw, raw[1:]))[:len(raw) // 2] or [0])
    d = {
        "channel": ch, "enabled": bool(c["en"]), "range": c["rng"], "coupling": c["cpl"], "probe": c["probe"],
        "calibrated": cal, "unit": unit, "rate_sps": rate, "record_s": 1024 / rate if rate else None,
        "range_tier": info["range_tier"][c["rng"]],
        "vdiv": (vpc * info["counts_per_div"]) if cal else None,
        "amp_top_base": amp, "vpp": m["vpp"], "mean": m["vavg"], "rms_ac": m["vrms_ac"],
        "top": m["vtop"], "base": m["vbase"], "freq_hz": statistics.median(freqs) if freqs else None,
        "duty": m.get("duty"), "span_counts": span_counts, "clipped": bool(m["clipped"]),
        "flags": [], "shape": None, "summary": "",
    }
    # ---- flags
    if not c["en"]:
        d["flags"].append("channel disabled")
    if d["clipped"]:
        d["flags"].append("clipped: signal exceeds this range, choose a larger V/div (autoset does this)")
    if span_counts < 6:
        d["flags"].append("no signal (or far below this range's resolution)")
    elif span_counts < 24:
        d["flags"].append("low resolution: only %d ADC counts of swing, use a finer range" % span_counts)
    if not cal:
        d["flags"].append("uncalibrated range %d: values are ADC counts, not volts" % c["rng"])
    elif d["range_tier"] == "provisional":
        d["flags"].append("range %d is provisionally calibrated (OpenScope); treat volts as +/-10 %%" % c["rng"])
    if freqs and len(freqs) >= 3:
        spread = (max(freqs) - min(freqs)) / statistics.median(freqs)
        if spread > 0.05:
            d["flags"].append("frequency unstable across frames (%.1f%% spread): intermittent contact, jitter or multiple tones" % (spread * 100))
    if len(freqs) < len(recs) // 2 and span_counts >= 24:
        d["flags"].append("periodicity not found on most frames: noise, DC with glitches, or frequency outside this timebase's reach")
    if d["freq_hz"] and 58 <= d["freq_hz"] <= 62 and cal and amp > 0.05:
        d["flags"].append("~60 Hz: mains pickup -- a floating/unconnected probe or missing ground clip looks exactly like this")
    # ---- shape
    if span_counts < 6:
        d["shape"] = "flat (DC level %s)" % fmt(d["mean"], unit)
    elif d["freq_hz"]:
        # histogram of the record: fraction near the two extremes vs middle
        lo_t, hi_t = mn + span_counts * 0.15, mx - span_counts * 0.15
        near_ext = sum(1 for x in raw if x <= lo_t or x >= hi_t) / len(raw)
        mean_c = sum(raw) / len(raw)                       # in counts, whatever the calibration
        rms_c = (m["vrms_ac"] / k) if cal else m["vrms_ac"]
        crest = max(mx - mean_c, mean_c - mn) / max(rms_c, 1e-9)
        duty = d["duty"] or 0.5
        if near_ext > 0.8:
            d["shape"] = "square/pulse" if abs(duty - 0.5) < 0.08 else "pulse (duty %.0f%%)" % (duty * 100)
        elif crest < 1.55:
            d["shape"] = "sine-like"
        elif crest < 1.9:
            d["shape"] = "triangle/ramp-like"
        else:
            d["shape"] = "spiky/pulsed (crest factor %.1f)" % crest
    else:
        d["shape"] = "aperiodic/noise-like"
    # ---- summary
    parts = ["CH%d" % ch, d["shape"]]
    if d["freq_hz"]:
        parts.append("%s" % fmt_f(d["freq_hz"]))
    parts.append("amplitude %s (top %s, base %s)" % (fmt(amp, unit), fmt(d["top"], unit), fmt(d["base"], unit)))
    parts.append("mean %s" % fmt(d["mean"], unit))
    parts.append("probe x%d %s, range %d%s, %s/div" % (c["probe"], c["cpl"], c["rng"],
                 "" if cal else " (uncal)", fmt(d["vdiv"], "V") if cal else "--"))
    d["summary"] = "; ".join(parts) + ("  FLAGS: " + " | ".join(d["flags"]) if d["flags"] else "")
    return d


def fmt(v, unit):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "--"
    if unit != "V":
        return "%.1f cnt" % v
    a = abs(v)
    return ("%.2f V" % v) if a >= 1 else ("%.0f mV" % (v * 1e3)) if a >= 1e-3 else ("%.0f uV" % (v * 1e6))


def fmt_f(f):
    return ("%.4g MHz" % (f / 1e6)) if f >= 1e6 else ("%.4g kHz" % (f / 1e3)) if f >= 1e3 else ("%.4g Hz" % f)


def fmt_t(s):
    a = abs(s)
    return ("%.3g s" % s) if a >= 1 else ("%.3g ms" % (s * 1e3)) if a >= 1e-3 else ("%.3g us" % (s * 1e6)) if a >= 1e-6 else ("%.3g ns" % (s * 1e9))


# ------------------------------------------------------------------ autoset
def autoset(ch, info, verbose=True):
    """Deterministic: find the finest range that holds the signal, a timebase that shows
    ~10 cycles, and a mid-level trigger. Returns the final description."""
    say = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)
    mvpc = info["mv_per_count"][str(ch)] if str(ch) in info["mv_per_count"] else info["mv_per_count"][ch - 1]
    cpd = info["counts_per_div"]
    cal_ranges = [r for r in range(10) if mvpc[r]]          # 4..9 on this unit
    # 1. coarse range first so nothing clips, then zoom in. The screen spans
    #    +/-4 divisions around 0 V (true-scale, no offset), so the signal's top AND
    #    base must both fit inside +/-3.6 div, not just its peak-to-peak.
    cmd("fpga scope vdiv %d %d" % (ch, max(cal_ranges)))
    cmd("fpga scope trigmode auto")
    clipped_at = set()
    d = None
    for step in range(6):
        d = analyze(frames(3), ch, info)
        rng = d["range"]
        if d["clipped"]:
            clipped_at.add(rng)
            up = [r for r in cal_ranges if r > rng]
            if not up:
                say("autoset: clipped even on the coarsest range -- signal too large for this input")
                break
            cmd("fpga scope vdiv %d %d" % (ch, up[0])); continue
        if d["span_counts"] < 6 and step == 0:
            cmd("fpga scope vdiv %d %d" % (ch, min(cal_ranges))); continue   # nothing visible: try the finest
        if not d["calibrated"]:
            break
        top, base = d["top"], d["base"]
        best = rng
        for r in sorted(cal_ranges):
            if r in clipped_at:
                continue
            vdiv = mvpc[r] / 1000 * cpd
            if top <= 3.6 * vdiv and base >= -3.6 * vdiv:
                best = r; break
        if best == rng:
            break
        cmd("fpga scope vdiv %d %d" % (ch, best))
    say("autoset: range %d (%s/div)%s" % (d["range"], fmt(d["vdiv"], "V") if d["calibrated"] else "--", " CLIPPED" if d["clipped"] else ""))
    # 2. timebase: aim for ~10 cycles per 1024-sample record
    tbs = sorted(info["timebases"], key=lambda t: -t["rate"])
    f0 = d["freq_hz"]
    if not f0:
        # sweep slower timebases looking for a period
        for t in tbs:
            cmd("fpga scope timebase 0x%02X" % t["code"])
            d = analyze(frames(3, settle=0.6), ch, info)
            if d["freq_hz"]:
                f0 = d["freq_hz"]; break
    if f0:
        want_rate = f0 * 1024 / 10.0
        t = min(tbs, key=lambda t: abs(math.log(t["rate"] / want_rate)))
        cmd("fpga scope timebase 0x%02X" % t["code"])
        say("autoset: timebase 0x%02X (%s, %s/div) for %s" % (t["code"], fmt_rate(t["rate"]), fmt_t(t["s_per_div"]), fmt_f(f0)))
    else:
        cmd("fpga scope timebase 0x10")
        say("autoset: no periodic signal found; timebase 0x10 (2.56 ms/div)")
    # 3. trigger at the signal's mid level (device units: pixel offset from centre, 216 px = 256 counts)
    d = analyze(frames(3, settle=0.6), ch, info)
    if d["span_counts"] >= 6:
        mid_counts = ((d["top"] + d["base"]) / 2) / (d["vpc"] if False else 1)
        raw = ch_fields(frames(1)[-1], ch)["raw"]
        mid = (max(raw) + min(raw)) / 2.0
        level = int(round((mid - 128) * 216 / 256))
        level = max(-100, min(100, level))
        cmd("fpga scope level %d" % level)
        cmd("fpga scope edge rising")
        say("autoset: trigger level %d (mid level), rising edge, auto mode" % level)
    return analyze(frames(3, settle=0.6), ch, info)


# ------------------------------------------------------------------ plotting
def plot(fs, chans, info, path, descs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    f = fs[-1]
    rate = f["rate"] or 0
    n = 1024
    t0 = -512 if f["hdr"].get("ordered") else 0
    xs = [((i + t0) / rate) for i in range(n)] if rate else list(range(n))
    xl = "time (s, 0 = trigger)" if (rate and f["hdr"].get("ordered")) else ("time (s)" if rate else "sample")
    fig, axes = plt.subplots(len(chans), 1, figsize=(11, 3.2 * len(chans)), sharex=True, squeeze=False)
    for ax, ch in zip(axes[:, 0], chans):
        c = ch_fields(f, ch)
        vals = [(b - 128) * c["vpc"] for b in c["raw"]] if c["vpc"] else list(c["raw"])
        ax.plot(xs, vals, color="#1fb4d8" if ch == 1 else "#2458e8", lw=1)
        ax.set_ylabel("CH%d (%s)" % (ch, "V" if c["vpc"] else "ADC counts"))
        ax.grid(True, alpha=.3)
        d = descs.get(ch)
        if d:
            ax.set_title(d["summary"][:150], fontsize=9, loc="left")
        if c["vpc"]:
            ax.axhline(0, color="k", lw=.5)
    axes[-1, 0].set_xlabel(xl)
    fig.suptitle("2C53T %s   %s   tb 0x%02X (%s)" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["t"])),
                 "triggered" if f["hdr"].get("ordered") else "free-run", f["hdr"]["tb"], fmt_rate(rate) if rate else "?"), fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    return path


# ------------------------------------------------------------------ main
def main():
    global URL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("wait"); p.add_argument("--ch", default="any"); p.add_argument("--timeout", type=float, default=120); p.add_argument("--counts", type=int, default=12)
    p = sub.add_parser("autoset"); p.add_argument("--ch", type=int, default=1); p.add_argument("--png")
    p = sub.add_parser("describe"); p.add_argument("--ch", default="both"); p.add_argument("--png"); p.add_argument("--frames", type=int, default=5); p.add_argument("--json", action="store_true")
    p = sub.add_parser("set"); p.add_argument("line")
    p = sub.add_parser("screen"); p.add_argument("out"); p.add_argument("--scale", type=int, default=2)
    p = sub.add_parser("log"); p.add_argument("action", choices=["start", "stop", "status", "csv", "clear"]); p.add_argument("out", nargs="?")
    a = ap.parse_args()
    URL = a.url
    info = api("/api/info")

    if a.cmd == "status":
        f = decode(api("/api/frame.json") or {})
        h = f.get("hdr", {})
        print("viewer:   %s  port %s  run=%s  fps %s  errors %s" % (info["status"], info["port"], info["run"], f.get("fps"), f.get("errors")))
        if h:
            print("scope:    tb 0x%02X (%s)  trig %s %s CH%d lvl %d  gen %s %s" % (h["tb"], fmt_rate(f["rate"]) if f.get("rate") else "?",
                  TMODE[h["tmode"]], "rising" if h["tedge"] == 0 else "falling", h["tsrc"] + 1, h["tlvl"], h["gen"], "triggered" if h["ordered"] else "free-run"))
            for ch in (1, 2):
                c = ch_fields(f, ch)
                print("CH%d:      %s range %d %s x%d %s  amp %s  mean %s  freq %s%s" % (ch, "on " if c["en"] else "off", c["rng"],
                      "(%s/div)" % fmt(c["vpc"] * info["counts_per_div"], "V") if c["vpc"] else "(uncal)", c["probe"], c["cpl"],
                      fmt(c["meas"]["vamp"], "V" if c["vpc"] else "cnt"), fmt(c["meas"]["vavg"], "V" if c["vpc"] else "cnt"),
                      fmt_f(c["meas"]["freq"]) if c["meas"].get("freq") else "--", "  CLIPPED" if c["meas"]["clipped"] else ""))
        lg = api("/api/log"); print("logger:   %s, %d rows" % ("ON" if lg["logging"] else "off", lg["rows"]))
        return

    if a.cmd == "wait":
        chans = [1, 2] if a.ch == "any" else [int(a.ch)]
        t0 = time.time()
        print("waiting for a signal on CH%s (>= %d counts of swing), up to %.0f s ..." % ("1/CH2" if a.ch == "any" else a.ch, a.counts, a.timeout))
        while time.time() - t0 < a.timeout:
            f = frames(1)[-1]
            for ch in chans:
                r = ch_fields(f, ch)["raw"]
                if max(r) - min(r) >= a.counts:
                    print("signal on CH%d after %.0f s (%d counts of swing)" % (ch, time.time() - t0, max(r) - min(r)))
                    return
            time.sleep(0.5)
        raise SystemExit("no signal within %.0f s" % a.timeout)

    if a.cmd == "autoset":
        d = autoset(a.ch, info)
        print(d["summary"])
        if a.png:
            plot(frames(1), [a.ch], info, a.png, {a.ch: d}); print("plot:", a.png)
        return

    if a.cmd == "describe":
        chans = [1, 2] if a.ch == "both" else [int(a.ch)]
        fs = frames(a.frames)
        descs = {ch: analyze(fs, ch, info) for ch in chans}
        if a.json:
            print(json.dumps(descs, indent=1, default=str))
        else:
            for ch in chans:
                print(descs[ch]["summary"])
        if a.png:
            plot(fs, chans, info, a.png, descs); print("plot:", a.png)
        return

    if a.cmd == "set":
        print(cmd(a.line))
        return

    if a.cmd == "screen":
        data = api("/api/screen.png?scale=%d" % a.scale, timeout=60)
        open(a.out, "wb").write(data); print("saved", a.out, len(data), "bytes")
        return

    if a.cmd == "log":
        if a.action in ("start", "stop", "clear"):
            print(api("/api/log", {"action": a.action}))
        elif a.action == "status":
            print(api("/api/log"))
        else:
            data = api("/api/log.csv", timeout=120)
            out = a.out or time.strftime("scope_log_%Y%m%d_%H%M%S.csv")
            open(out, "wb").write(data); print("saved", out, len(data), "bytes")


if __name__ == "__main__":
    main()
