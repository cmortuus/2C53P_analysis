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


# ------------------------------------------------------------------ pulse metrics
def edges(raw, hyst=0.15):
    """Rising and falling mean-level crossings (sub-sample), with hysteresis."""
    mn, mx = min(raw), max(raw)
    span = mx - mn
    if span < 6:
        return [], [], None
    mean = sum(raw) / len(raw)
    hi, lo = mean + hyst * span, mean - hyst * span
    rises, falls = [], []
    state = raw[0] > mean
    for i in range(1, len(raw)):
        x = raw[i]
        if state and x < lo:
            state = False
            j = i
            while j > 0 and raw[j - 1] < mean: j -= 1
            falls.append(j - 1 + (mean - raw[j - 1]) / (raw[j] - raw[j - 1]) if j > 0 and raw[j] != raw[j - 1] else float(j))
        elif not state and x > hi:
            state = True
            j = i
            while j > 0 and raw[j - 1] > mean: j -= 1
            rises.append(j - 1 + (mean - raw[j - 1]) / (raw[j] - raw[j - 1]) if j > 0 and raw[j] != raw[j - 1] else float(j))
    return rises, falls, mean


def transition_time(raw, pos, rising, mn, mx):
    """10%-90% time (in samples) around an edge at `pos`, or None if unresolved."""
    lo_t, hi_t = mn + 0.1 * (mx - mn), mn + 0.9 * (mx - mn)
    i = int(pos)
    a = b = None
    for k in range(max(1, i - 40), min(len(raw) - 1, i + 41)):
        y0, y1 = raw[k - 1], raw[k]
        if rising:
            if a is None and y0 < lo_t <= y1: a = k - 1 + (lo_t - y0) / (y1 - y0)
            if a is not None and y0 < hi_t <= y1: b = k - 1 + (hi_t - y0) / (y1 - y0); break
        else:
            if a is None and y0 > hi_t >= y1: a = k - 1 + (hi_t - y0) / (y1 - y0)
            if a is not None and y0 > lo_t >= y1: b = k - 1 + (lo_t - y0) / (y1 - y0); break
    return (b - a) if (a is not None and b is not None and b > a) else None


def pulse_metrics(raw, rate):
    rises, falls, mean = edges(raw)
    if len(rises) < 2 and len(falls) < 2:
        return None
    mn, mx = min(raw), max(raw)
    highs, lows = [], []
    for r in rises:
        f = next((f for f in falls if f > r), None)
        if f is not None: highs.append(f - r)
    for f in falls:
        r = next((r for r in rises if r > f), None)
        if r is not None: lows.append(r - f)
    def med(v): return statistics.median(v) if v else None
    rt = [t for t in (transition_time(raw, r, True, mn, mx) for r in rises[:50]) if t]
    ft = [t for t in (transition_time(raw, f, False, mn, mx) for f in falls[:50]) if t]
    glitches = 0
    for w, ref in ((highs, med(highs)), (lows, med(lows))):
        if ref:
            glitches += sum(1 for x in w if x < 0.2 * ref)
    per = (1.0 / rate) if rate else None
    out = {"rising_edges": len(rises), "falling_edges": len(falls),
           "high_s": med(highs) * per if (per and highs) else None, "low_s": med(lows) * per if (per and lows) else None,
           "high_samples": med(highs), "low_samples": med(lows),
           "rise_s": med(rt) * per if (per and rt) else None, "fall_s": med(ft) * per if (per and ft) else None,
           "rise_samples": med(rt), "fall_samples": med(ft), "glitches": glitches,
           "edge_resolution_s": (2 * per) if per else None}
    return out


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
        "flags": [], "shape": None, "summary": "", "pulse": pulse_metrics(raw, rate),
        "zero_counts": fs[-1].get("zero%d" % ch, 0.0), "gain": (info.get("calib", {}).get("gain", {}).get(str(ch), {}).get(str(c["rng"]), 1.0)),
    }
    pm = d["pulse"]
    if pm and pm["glitches"]:
        d["flags"].append("%d runt pulse(s) shorter than 20%% of the normal width: glitches or contact bounce" % pm["glitches"])
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
    # ---- spectral peaks (numpy; Hann; top 3 distinct peaks, dB re the strongest)
    d["spectrum_peaks"] = []
    if rate and span_counts >= 6:
        try:
            import numpy as np
            x = (np.frombuffer(bytes(raw), dtype=np.uint8).astype(float) - sum(raw) / len(raw)) * np.hanning(len(raw))
            mag = np.abs(np.fft.rfft(x))
            mag[0] = 0
            order = np.argsort(mag)[::-1]
            peaks, taken = [], []
            for k in order:
                if mag[k] <= 0 or any(abs(k - t) < 4 for t in taken):
                    continue
                taken.append(int(k))
                k0 = int(k)
                if 1 <= k0 < len(mag) - 1 and mag[k0 - 1] > 0 and mag[k0 + 1] > 0:
                    a, b, c2 = np.log(mag[k0 - 1]), np.log(mag[k0]), np.log(mag[k0 + 1])
                    dd = (a - c2) / (2 * (a - 2 * b + c2)) if (a - 2 * b + c2) != 0 else 0.0
                    kk = k0 + (dd if abs(dd) < 1 else 0.0)
                else:
                    kk = float(k0)
                peaks.append({"hz": kk * rate / len(raw), "db_rel": 20 * math.log10(mag[k0] / mag[order[0]])})
                if len(peaks) == 3:
                    break
            d["spectrum_peaks"] = peaks
        except Exception:
            pass
    # ---- near the input limit for this probe setting?
    if cal:
        limit = 40.0 * c["probe"]
        if max(abs(d["top"]), abs(d["base"])) > 0.8 * limit:
            d["flags"].append("within 20%% of the input limit for a x%d probe (%g V): stop and switch to x10 / check the source" % (c["probe"], limit))
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
    if d["pulse"] and d["pulse"]["high_s"] and d["freq_hz"]:
        pm = d["pulse"]
        tt = []
        for nm, v, samp in (("rise", pm["rise_s"], pm["rise_samples"]), ("fall", pm["fall_s"], pm["fall_samples"])):
            if v is None or (samp is not None and samp < 2):
                tt.append("%s <%s (unresolved at this rate)" % (nm, fmt_t(pm["edge_resolution_s"])))
            else:
                tt.append("%s %s" % (nm, fmt_t(v)))
        parts.append("high %s / low %s, %s" % (fmt_t(pm["high_s"]), fmt_t(pm["low_s"]), ", ".join(tt)))
    parts.append("probe x%d %s, range %d%s, %s/div" % (c["probe"], c["cpl"], c["rng"],
                 "" if cal else " (uncal)", fmt(d["vdiv"], "V") if cal else "--"))
    if d["zero_counts"] or d["gain"] != 1.0:
        parts.append("unit cal applied (gain x%.3f, zero %+.1f cnt)" % (d["gain"], d["zero_counts"]))
    if d.get("spectrum_peaks"):
        parts.append("spectrum peaks " + ", ".join("%s (%+.0f dB)" % (fmt_f(pk["hz"]), pk["db_rel"]) for pk in d["spectrum_peaks"]))
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


# ------------------------------------------------------------------ two-channel timing
def timing(fs, info):
    """Delay/phase between CH1 and CH2 rising edges when both carry the same frequency."""
    f = fs[-1]
    rate = f["rate"]
    if not rate:
        return None
    r1, _, _ = edges(f["b1"]); r2, _, _ = edges(f["b2"])
    if len(r1) < 2 or len(r2) < 2:
        return None
    p1 = statistics.median(b - a for a, b in zip(r1, r1[1:]))
    p2 = statistics.median(b - a for a, b in zip(r2, r2[1:]))
    if abs(p1 - p2) / max(p1, p2) > 0.03:
        return {"same_freq": False, "f1": rate / p1, "f2": rate / p2}
    ds = []
    for a in r1:
        cands = [b - a for b in r2 if -p1 / 2 <= b - a < p1 / 2]
        if cands:
            ds.append(min(cands, key=abs))
    if not ds:
        return None
    d = statistics.median(ds)
    phase = d / p1 * 360.0
    out = {"same_freq": True, "period_s": p1 / rate, "delay_s": d / rate, "phase_deg": phase, "n": len(ds)}
    if 60 < abs(phase) < 120:
        out["quadrature"] = "CH2 %s CH1 by ~90 deg (%s)" % ("lags" if phase > 0 else "leads", "A->B order" if phase > 0 else "B->A order")
    return out


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
        z = f.get("zero%d" % ch, 0.0) or 0.0
        vals = [(b - 128 - z) * c["vpc"] for b in c["raw"]] if c["vpc"] else list(c["raw"])
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
    p = sub.add_parser("autoset"); p.add_argument("--ch", default="1", help="1, 2 or both"); p.add_argument("--png")
    p = sub.add_parser("report"); p.add_argument("dir"); p.add_argument("--ch", default="both"); p.add_argument("--no-autoset", action="store_true"); p.add_argument("--title", default="")
    p = sub.add_parser("describe"); p.add_argument("--ch", default="both"); p.add_argument("--png"); p.add_argument("--frames", type=int, default=5); p.add_argument("--json", action="store_true")
    p = sub.add_parser("set"); p.add_argument("line")
    p = sub.add_parser("screen"); p.add_argument("out"); p.add_argument("--scale", type=int, default=2)
    p = sub.add_parser("log"); p.add_argument("action", choices=["start", "stop", "status", "csv", "clear"]); p.add_argument("out", nargs="?"); p.add_argument("--summary", action="store_true")
    p = sub.add_parser("zero", help="probe tip on GROUND: store this channel/range's zero offset on the Pi"); p.add_argument("--ch", type=int, required=True)
    p = sub.add_parser("gain", help="known amplitude applied: store this channel/range's gain correction"); p.add_argument("--ch", type=int, required=True); p.add_argument("--expected", type=float, required=True, help="true top-base amplitude in volts")
    p = sub.add_parser("calib"); p.add_argument("action", choices=["show", "clear"]); p.add_argument("--ch", type=int); p.add_argument("--rng", type=int)
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
        chans = [1, 2] if a.ch == "both" else [int(a.ch)]
        descs = {}
        for ch in chans:
            descs[ch] = autoset(ch, info)
        if len(chans) == 2:
            # one timebase for both: follow the faster periodic signal (step, not dir)
            fr = [descs[c]["freq_hz"] for c in chans if descs[c]["freq_hz"]]
            if fr:
                tbs = sorted(info["timebases"], key=lambda t: -t["rate"])
                want = max(fr) * 1024 / 10.0
                t = min(tbs, key=lambda t: abs(math.log(t["rate"] / want)))
                cmd("fpga scope timebase 0x%02X" % t["code"])
                fs = frames(3, settle=0.6)
                descs = {c: analyze(fs, c, info) for c in chans}
        for ch in chans:
            print(descs[ch]["summary"])
        if a.png:
            plot(frames(1), chans, info, a.png, descs); print("plot:", a.png)
        return

    if a.cmd == "report":
        chans = [1, 2] if a.ch == "both" else [int(a.ch)]
        os.makedirs(a.dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = ["# Scope report %s" % (a.title or stamp), "", "generated %s by scopectl against %s" % (stamp, URL), ""]
        if not a.no_autoset:
            for ch in chans:
                autoset(ch, info, verbose=False)
            if len(chans) == 2:
                fs = frames(3, settle=0.6)
                fr = [analyze(fs, c, info)["freq_hz"] for c in chans]
                fr = [x for x in fr if x]
                if fr:
                    tbs = sorted(info["timebases"], key=lambda t: -t["rate"])
                    want = max(fr) * 1024 / 10.0
                    t = min(tbs, key=lambda t: abs(math.log(t["rate"] / want)))
                    cmd("fpga scope timebase 0x%02X" % t["code"])
        fs = frames(5, settle=0.6)
        descs = {ch: analyze(fs, ch, info) for ch in chans}
        f = fs[-1]
        lines.append("Settings: timebase 0x%02X (%s), trigger %s %s CH%d" % (f["hdr"]["tb"], fmt_rate(f["rate"]) if f["rate"] else "uncal",
                     TMODE[f["hdr"]["tmode"]], "rising" if f["hdr"]["tedge"] == 0 else "falling", f["hdr"]["tsrc"] + 1))
        lines.append("")
        for ch in chans:
            d = descs[ch]
            lines.append("## CH%d" % ch)
            lines.append("- " + d["summary"])
            if d["pulse"]:
                pm = d["pulse"]
                lines.append("- edges in record: %d rising / %d falling; glitches: %d" % (pm["rising_edges"], pm["falling_edges"], pm["glitches"]))
            for fl in d["flags"]:
                lines.append("- FLAG: " + fl)
            lines.append("")
        tm = timing(fs, info) if len(chans) == 2 else None
        if tm:
            lines.append("## CH1 -> CH2 timing")
            if tm.get("same_freq"):
                lines.append("- CH2 rising edge %s after CH1's (%.0f deg of %s), n=%d%s" % (fmt_t(tm["delay_s"]), tm["phase_deg"], fmt_t(tm["period_s"]), tm["n"],
                             ("; " + tm["quadrature"]) if tm.get("quadrature") else ""))
            else:
                lines.append("- different frequencies: %s vs %s" % (fmt_f(tm["f1"]), fmt_f(tm["f2"])))
            lines.append("")
        png = os.path.join(a.dir, "record.png")
        plot(fs, chans, info, png, descs)
        lines.append("![record](record.png)")
        try:
            data = api("/api/screen.png?scale=2", timeout=60)
            open(os.path.join(a.dir, "lcd.png"), "wb").write(data)
            lines.append("")
            lines.append("![device screen](lcd.png)")
        except Exception as e:
            lines.append("(device screen capture failed: %s)" % e)
        with open(os.path.join(a.dir, "raw.json"), "w") as fh:
            json.dump({"frame": {k: v for k, v in f.items() if k not in ("b1", "b2")}, "channels": descs, "timing": tm}, fh, indent=1, default=str)
        md = os.path.join(a.dir, "report.md")
        open(md, "w").write("\n".join(lines) + "\n")
        print("\n".join(lines))
        print("\nwritten:", md)
        return

    if a.cmd == "describe":
        chans = [1, 2] if a.ch == "both" else [int(a.ch)]
        fs = frames(a.frames)
        descs = {ch: analyze(fs, ch, info) for ch in chans}
        tm = timing(fs, info) if len(chans) == 2 else None
        if a.json:
            print(json.dumps({"channels": descs, "timing": tm}, indent=1, default=str))
        else:
            for ch in chans:
                print(descs[ch]["summary"])
            if tm:
                if tm.get("same_freq"):
                    print("CH1->CH2 timing: CH2 rising edge %s after CH1's (%.0f deg of a %s period, n=%d)%s" % (
                        fmt_t(tm["delay_s"]), tm["phase_deg"], fmt_t(tm["period_s"]), tm["n"],
                        ("  " + tm["quadrature"]) if tm.get("quadrature") else ""))
                else:
                    print("CH1->CH2 timing: different frequencies (%s vs %s)" % (fmt_f(tm["f1"]), fmt_f(tm["f2"])))
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

    if a.cmd == "zero":
        fs = frames(5)
        c = ch_fields(fs[-1], a.ch)
        means = [sum(ch_fields(f, a.ch)["raw"]) / 1024 for f in fs]
        spans = [max(ch_fields(f, a.ch)["raw"]) - min(ch_fields(f, a.ch)["raw"]) for f in fs]
        if max(spans) > 12:
            raise SystemExit("zero: the input is not quiet (%d counts of swing) -- put the tip on ground first" % max(spans))
        z = statistics.median(means) - 128.0
        api("/api/calib", {"ch": a.ch, "rng": c["rng"], "zero": z})
        print("CH%d range %d: zero offset %+.1f counts (%s) stored on the Pi" % (a.ch, c["rng"], z, fmt(z * c["vpc"], "V") if c["vpc"] else "uncal"))
        return

    if a.cmd == "gain":
        fs = frames(5)
        c = ch_fields(fs[-1], a.ch)
        if not c["vpc"]:
            raise SystemExit("gain: range %d is uncalibrated in the firmware; nothing to correct" % c["rng"])
        amps = [ch_fields(f, a.ch)["meas"]["vamp"] for f in fs]
        amp = statistics.median(amps)
        if amp <= 0 or any(ch_fields(f, a.ch)["meas"]["clipped"] for f in fs):
            raise SystemExit("gain: signal clipped or absent (amp %s)" % fmt(amp, "V"))
        old = info.get("calib", {}).get("gain", {}).get(str(a.ch), {}).get(str(c["rng"]), 1.0)
        new = old * a.expected / amp
        api("/api/calib", {"ch": a.ch, "rng": c["rng"], "gain": new})
        print("CH%d range %d: measured %s for expected %s -> gain x%.4f (was x%.4f) stored on the Pi" % (a.ch, c["rng"], fmt(amp, "V"), fmt(a.expected, "V"), new, old))
        return

    if a.cmd == "calib":
        if a.action == "clear":
            if a.ch is None or a.rng is None:
                raise SystemExit("calib clear needs --ch and --rng")
            print(api("/api/calib", {"ch": a.ch, "rng": a.rng, "clear": True}))
        else:
            print(json.dumps(api("/api/calib"), indent=1))
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
            if a.summary:
                import csv, io
                rows = list(csv.DictReader(io.StringIO(data.decode())))
                if not rows:
                    print("(empty log)"); return
                print("%d rows, %s .. %s" % (len(rows), rows[0]["t_iso"], rows[-1]["t_iso"]))
                for col in ("ch1_amp", "ch1_mean", "ch1_rms_ac", "ch1_freq", "ch2_amp", "ch2_mean", "ch2_rms_ac", "ch2_freq"):
                    vals = [(float(r[col]), r["t_iso"]) for r in rows if r[col] not in ("", None)]
                    if not vals: continue
                    lo, hi = min(vals), max(vals)
                    print("  %-11s mean %-10.4g min %-10.4g at %s   max %-10.4g at %s" % (col, sum(v for v, _ in vals) / len(vals), lo[0], lo[1][11:], hi[0], hi[1][11:]))


if __name__ == "__main__":
    main()
