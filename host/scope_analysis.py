"""scope_analysis — waveform analysis shared by the Pi server (/api/analyze) and scopectl.

Pure functions over raw 8-bit records; no I/O, no API. Every threshold is a parameter
(`Params`) so stored records can be re-evaluated post hoc with different criteria.

A "record" here is: raw (bytes, 1024 samples, ADC counts 0..255), rate (S/s or 0),
vpc (volts per count after the unit's gain correction, 0 = uncalibrated),
zero (counts to subtract before scaling, from the unit's zero calibration).
"""
import math
import statistics

try:
    import numpy as np
except Exception:          # the Pi may lack numpy; spectrum is then skipped
    np = None


class Params:
    """Evaluation criteria. Change these and re-run on stored records."""
    def __init__(self, **kw):
        self.hyst = 0.15            # edge hysteresis, fraction of the record's span
        self.min_span = 6           # counts of swing below which the record is "no signal"
        self.low_res = 24           # counts of swing below which "low resolution"
        self.glitch_ratio = 0.2     # pulse shorter than this * median width = runt
        self.freq_spread = 0.05     # relative spread across frames flagged as unstable
        self.mains_band = (58.0, 62.0)
        self.input_limit_v = 40.0   # per x1 probe; scaled by the probe factor
        self.limit_fraction = 0.8
        self.square_extreme_frac = 0.8   # fraction of samples near the extremes => square/pulse
        self.sine_crest_max = 1.55
        self.tri_crest_max = 1.9
        for k, v in kw.items():
            if not hasattr(self, k):
                raise ValueError("unknown parameter %s" % k)
            setattr(self, k, v)

    def as_dict(self):
        return dict(vars(self))


# ------------------------------------------------------------------ basic levels
def levels(raw, vpc=0.0, zero=0.0):
    """top/base = medians of the samples above/below the mean; vpp = max-min; mean; AC rms."""
    n = len(raw)
    mean = sum(raw) / n
    above = sorted(x for x in raw if x > mean) or [mean]
    below = sorted(x for x in raw if x <= mean) or [mean]
    top_c, base_c = above[len(above) // 2], below[len(below) // 2]
    var = sum((x - mean) ** 2 for x in raw) / n
    mn, mx = min(raw), max(raw)
    k = vpc if vpc else 1.0
    z = (128 + zero) if vpc else 0
    return {"unit": "V" if vpc else "cnt",
            "vmax": (mx - z) * k, "vmin": (mn - z) * k, "vpp": (mx - mn) * k,
            "vtop": (top_c - z) * k, "vbase": (base_c - z) * k, "vamp": (top_c - base_c) * k,
            "vavg": (mean - z) * k, "vrms_ac": math.sqrt(var) * k,
            "clipped": bool(mn <= 0 or mx >= 227), "span_counts": mx - mn, "mean_counts": mean}


# ------------------------------------------------------------------ edges / period
def edges(raw, hyst=0.15, min_span=6):
    """Rising and falling mean-level crossings (sub-sample), with hysteresis."""
    mn, mx = min(raw), max(raw)
    span = mx - mn
    if span < min_span:
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
            while j > 0 and raw[j - 1] < mean:
                j -= 1
            falls.append(j - 1 + (mean - raw[j - 1]) / (raw[j] - raw[j - 1]) if j > 0 and raw[j] != raw[j - 1] else float(j))
        elif not state and x > hi:
            state = True
            j = i
            while j > 0 and raw[j - 1] > mean:
                j -= 1
            rises.append(j - 1 + (mean - raw[j - 1]) / (raw[j] - raw[j - 1]) if j > 0 and raw[j] != raw[j - 1] else float(j))
    return rises, falls, mean


def period_samples(rises):
    """Seam-tolerant period: mean of the cycle periods that agree with their median."""
    if len(rises) < 3:
        return None
    pers = sorted(b - a for a, b in zip(rises, rises[1:]))
    med = pers[len(pers) // 2] if len(pers) % 2 else 0.5 * (pers[len(pers) // 2 - 1] + pers[len(pers) // 2])
    good = [q for q in pers if abs(q - med) <= max(1.5, 0.15 * med)]
    per = sum(good) / len(good) if good else med
    return per if per >= 2.0 else None


def frequency(raw, rate, p=None):
    p = p or Params()
    rises, _, _ = edges(raw, p.hyst, p.min_span)
    per = period_samples(rises)
    return (rate / per) if (per and rate) else None


def transition_time(raw, pos, rising, base, top):
    """10%-90% time in samples around an edge at `pos`, or None if unresolved.
    base/top are the flat levels (medians), so overshoot spikes do not stretch the window."""
    lo_t, hi_t = base + 0.1 * (top - base), base + 0.9 * (top - base)
    i = int(pos)
    a = b = None
    for k in range(max(1, i - 40), min(len(raw) - 1, i + 41)):
        y0, y1 = raw[k - 1], raw[k]
        if rising:
            if a is None and y0 < lo_t <= y1:
                a = k - 1 + (lo_t - y0) / (y1 - y0)
            if a is not None and y0 < hi_t <= y1:
                b = k - 1 + (hi_t - y0) / (y1 - y0)
                break
        else:
            if a is None and y0 > hi_t >= y1:
                a = k - 1 + (hi_t - y0) / (y1 - y0)
            if a is not None and y0 > lo_t >= y1:
                b = k - 1 + (lo_t - y0) / (y1 - y0)
                break
    return (b - a) if (a is not None and b is not None and b > a) else None


def pulse_metrics(raw, rate, p=None):
    p = p or Params()
    rises, falls, mean = edges(raw, p.hyst, p.min_span)
    if len(rises) < 2 and len(falls) < 2:
        return None
    mean_c = sum(raw) / len(raw)
    above = sorted(x for x in raw if x > mean_c) or [mean_c]
    below = sorted(x for x in raw if x <= mean_c) or [mean_c]
    top, base = above[len(above) // 2], below[len(below) // 2]
    highs, lows = [], []
    for r in rises:
        f = next((f for f in falls if f > r), None)
        if f is not None:
            highs.append(f - r)
    for f in falls:
        r = next((r for r in rises if r > f), None)
        if r is not None:
            lows.append(r - f)

    def med(v):
        return statistics.median(v) if v else None
    rt = [t for t in (transition_time(raw, r, True, base, top) for r in rises[:50]) if t]
    ft = [t for t in (transition_time(raw, f, False, base, top) for f in falls[:50]) if t]
    glitches = 0
    for w, ref in ((highs, med(highs)), (lows, med(lows))):
        if ref:
            glitches += sum(1 for x in w if x < p.glitch_ratio * ref)
    per = (1.0 / rate) if rate else None
    return {"rising_edges": len(rises), "falling_edges": len(falls),
            "high_s": med(highs) * per if (per and highs) else None, "low_s": med(lows) * per if (per and lows) else None,
            "high_samples": med(highs), "low_samples": med(lows),
            "rise_s": med(rt) * per if (per and rt) else None, "fall_s": med(ft) * per if (per and ft) else None,
            "rise_samples": med(rt), "fall_samples": med(ft), "glitches": glitches,
            "duty": (med(highs) / (med(highs) + med(lows))) if (highs and lows) else None,
            "edge_resolution_s": (2 * per) if per else None}


# ------------------------------------------------------------------ spectrum
def spectrum_peaks(raw, rate, n_peaks=3):
    if np is None or not rate:
        return []
    x = (np.frombuffer(bytes(raw), dtype=np.uint8).astype(float) - sum(raw) / len(raw)) * np.hanning(len(raw))
    mag = np.abs(np.fft.rfft(x))
    mag[0] = 0
    if float(mag.max()) <= 0:
        return []
    order = np.argsort(mag)[::-1]
    peaks, taken = [], []
    for k in order:
        k0 = int(k)
        if mag[k0] <= 0 or any(abs(k0 - t) < 4 for t in taken):
            continue
        taken.append(k0)
        kk = float(k0)
        if 1 <= k0 < len(mag) - 1 and mag[k0 - 1] > 0 and mag[k0 + 1] > 0:
            a, b, c2 = math.log(mag[k0 - 1]), math.log(mag[k0]), math.log(mag[k0 + 1])
            den = a - 2 * b + c2
            if den != 0:
                dd = (a - c2) / (2 * den)
                if abs(dd) < 1:
                    kk = k0 + dd
        peaks.append({"hz": kk * rate / len(raw), "db_rel": 20 * math.log10(float(mag[k0]) / float(mag[order[0]]))})
        if len(peaks) == n_peaks:
            break
    return peaks


# ------------------------------------------------------------------ formatting
def fmt(v, unit):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "--"
    if unit != "V":
        return "%.1f cnt" % v
    a = abs(v)
    return ("%.2f V" % v) if a >= 1 else ("%.0f mV" % (v * 1e3)) if a >= 1e-3 else ("%.0f uV" % (v * 1e6))


def fmt_f(f):
    if f is None:
        return "--"
    return ("%.4g MHz" % (f / 1e6)) if f >= 1e6 else ("%.4g kHz" % (f / 1e3)) if f >= 1e3 else ("%.4g Hz" % f)


def fmt_t(s):
    if s is None:
        return "--"
    a = abs(s)
    return ("%.3g s" % s) if a >= 1 else ("%.3g ms" % (s * 1e3)) if a >= 1e-3 else ("%.3g us" % (s * 1e6)) if a >= 1e-6 else ("%.3g ns" % (s * 1e9))


def fmt_rate(r):
    return ("%.4g MS/s" % (r / 1e6)) if r >= 1e6 else ("%.4g kS/s" % (r / 1e3)) if r >= 1e3 else ("%.4g S/s" % r)


# ------------------------------------------------------------------ full description
def analyze_records(recs, p=None):
    """recs: list of dicts, oldest first, each with raw, rate, vpc, zero, probe (1|10), cpl ('DC'|'AC'|'GND'),
    rng, tier ('measured'|'provisional'|'none'), enabled (bool), channel (1|2), counts_per_div, gain.
    Uses the LAST record for the shape and levels, all records for stability. Returns a dict."""
    p = p or Params()
    c = recs[-1]
    raw, rate, vpc, zero = c["raw"], c["rate"], c["vpc"], c.get("zero", 0.0)
    cal = vpc > 0
    unit = "V" if cal else "cnt"
    lv = levels(raw, vpc, zero)
    amps = [levels(r["raw"], r["vpc"], r.get("zero", 0.0))["vamp"] for r in recs]
    freqs = [f for f in (frequency(r["raw"], r["rate"], p) for r in recs) if f]
    pm = pulse_metrics(raw, rate, p)
    d = {
        "channel": c.get("channel"), "enabled": bool(c.get("enabled", True)), "range": c["rng"], "coupling": c.get("cpl"),
        "probe": c.get("probe", 1), "calibrated": cal, "unit": unit, "rate_sps": rate,
        "record_s": len(raw) / rate if rate else None, "range_tier": c.get("tier"),
        "vdiv": (vpc * c.get("counts_per_div", 32)) if cal else None,
        "amp_top_base": statistics.median(amps), "vpp": lv["vpp"], "mean": lv["vavg"], "rms_ac": lv["vrms_ac"],
        "top": lv["vtop"], "base": lv["vbase"], "freq_hz": statistics.median(freqs) if freqs else None,
        "duty": pm["duty"] if pm else None, "span_counts": lv["span_counts"], "clipped": lv["clipped"],
        "pulse": pm, "zero_counts": zero, "gain": c.get("gain", 1.0), "frames": len(recs),
        "spectrum_peaks": spectrum_peaks(raw, rate) if lv["span_counts"] >= p.min_span else [],
        "flags": [], "shape": None, "summary": "", "params": p.as_dict(),
    }
    span = lv["span_counts"]
    if not d["enabled"]:
        d["flags"].append("channel disabled")
    if d["clipped"]:
        d["flags"].append("clipped: signal exceeds this range, choose a larger V/div")
    if span < p.min_span:
        d["flags"].append("no signal (or far below this range's resolution)")
    elif span < p.low_res:
        d["flags"].append("low resolution: only %d ADC counts of swing, use a finer range" % span)
    if not cal:
        d["flags"].append("uncalibrated range %d: values are ADC counts, not volts" % c["rng"])
    elif d["range_tier"] == "provisional":
        d["flags"].append("range %d is provisionally calibrated; treat volts as +/-10 %%" % c["rng"])
    if len(freqs) >= 3:
        spread = (max(freqs) - min(freqs)) / statistics.median(freqs)
        if spread > p.freq_spread:
            d["flags"].append("frequency unstable across frames (%.1f%% spread): intermittent contact, jitter or multiple tones" % (spread * 100))
    if len(freqs) < len(recs) // 2 and span >= p.low_res:
        d["flags"].append("periodicity not found on most frames: noise, DC with glitches, or frequency outside this timebase's reach")
    if d["freq_hz"] and p.mains_band[0] <= d["freq_hz"] <= p.mains_band[1] and cal and d["amp_top_base"] > 0.05:
        d["flags"].append("~60 Hz: mains pickup -- a floating/unconnected probe or missing ground clip looks exactly like this")
    if pm and pm["glitches"]:
        d["flags"].append("%d runt pulse(s) shorter than %.0f%% of the normal width: glitches or contact bounce" % (pm["glitches"], p.glitch_ratio * 100))
    if cal:
        limit = p.input_limit_v * d["probe"]
        if max(abs(d["top"]), abs(d["base"])) > p.limit_fraction * limit:
            d["flags"].append("within %.0f%% of the input limit for a x%d probe (%g V): stop and switch to x10 / check the source"
                              % ((1 - p.limit_fraction) * 100, d["probe"], limit))
    # shape
    if span < p.min_span:
        d["shape"] = "flat (DC level %s)" % fmt(d["mean"], unit)
    elif d["freq_hz"]:
        mn, mx = min(raw), max(raw)
        mean_c = lv["mean_counts"]
        above = sorted(x for x in raw if x > mean_c) or [mean_c]
        below = sorted(x for x in raw if x <= mean_c) or [mean_c]
        top_c, base_c = above[len(above) // 2], below[len(below) // 2]
        lvl_span = max(top_c - base_c, 1)
        lo_t, hi_t = base_c + lvl_span * 0.15, top_c - lvl_span * 0.15
        near_ext = sum(1 for x in raw if x <= lo_t or x >= hi_t) / len(raw)
        rms_c = (lv["vrms_ac"] / vpc) if cal else lv["vrms_ac"]
        crest = max(top_c - mean_c, mean_c - base_c) / max(rms_c, 1e-9)
        over = max(mx - top_c, base_c - mn) / lvl_span
        d["overshoot"] = over
        if over > 0.15:
            d["flags"].append("overshoot/ringing %.0f%% of the step: fast edges into the probe's capacitance or a real ringing source" % (over * 100))
        duty = d["duty"] if d["duty"] is not None else 0.5
        if near_ext > p.square_extreme_frac:
            d["shape"] = "square/pulse" if abs(duty - 0.5) < 0.08 else "pulse (duty %.0f%%)" % (duty * 100)
        elif crest < p.sine_crest_max:
            d["shape"] = "sine-like"
        elif crest < p.tri_crest_max:
            d["shape"] = "triangle/ramp-like"
        else:
            d["shape"] = "spiky/pulsed (crest factor %.1f)" % crest
    else:
        d["shape"] = "aperiodic/noise-like"
    # summary
    parts = ["CH%s" % d["channel"], d["shape"]]
    if d["freq_hz"]:
        parts.append(fmt_f(d["freq_hz"]))
    parts.append("amplitude %s (top %s, base %s)" % (fmt(d["amp_top_base"], unit), fmt(d["top"], unit), fmt(d["base"], unit)))
    parts.append("mean %s" % fmt(d["mean"], unit))
    if pm and pm["high_s"] and d["freq_hz"]:
        tt = []
        for nm, v, samp in (("rise", pm["rise_s"], pm["rise_samples"]), ("fall", pm["fall_s"], pm["fall_samples"])):
            if v is None or (samp is not None and samp < 2):
                tt.append("%s <%s (unresolved at this rate)" % (nm, fmt_t(pm["edge_resolution_s"])))
            else:
                tt.append("%s %s" % (nm, fmt_t(v)))
        parts.append("high %s / low %s, %s" % (fmt_t(pm["high_s"]), fmt_t(pm["low_s"]), ", ".join(tt)))
    parts.append("probe x%d %s, range %d%s, %s/div" % (d["probe"], d["coupling"], c["rng"], "" if cal else " (uncal)",
                 fmt(d["vdiv"], "V") if cal else "--"))
    if zero or d["gain"] != 1.0:
        parts.append("unit cal applied (gain x%.3f, zero %+.1f cnt)" % (d["gain"], zero))
    if d["spectrum_peaks"]:
        parts.append("spectrum peaks " + ", ".join("%s (%+.0f dB)" % (fmt_f(pk["hz"]), pk["db_rel"]) for pk in d["spectrum_peaks"]))
    d["summary"] = "; ".join(parts) + ("  FLAGS: " + " | ".join(d["flags"]) if d["flags"] else "")
    return d


def timing(raw1, raw2, rate, p=None):
    """Delay/phase between CH1 and CH2 rising edges when both carry the same frequency."""
    p = p or Params()
    if not rate:
        return None
    r1, _, _ = edges(raw1, p.hyst, p.min_span)
    r2, _, _ = edges(raw2, p.hyst, p.min_span)
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
