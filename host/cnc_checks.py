"""cnc_checks — pass/fail checks for CNC controller bring-up (Mesa card + LinuxCNC + drives).

Each check turns scope records into numbers and judges them against a spec. A check that is
closer to its limit than the scope can resolve is reported MARGINAL rather than PASS/FAIL, so a
verdict is never stronger than the measurement.

  step_dir      step pulse width / space / rate, DIR setup and hold (CH1 step, CH2 dir)
  encoder       quadrature A/B: phase, duty, closest A-B edge spacing, speed
  logic_levels  are the levels and the noise on a line inside a receiver's thresholds
  rail          supply level, ripple and sag over many frames
  bounce        switch/contact bounce on a triggered record

Inputs are the continuous part of each record (scopectl's rec_from_frame already cuts the junk
block, the same cut on both channels). Times are seconds, levels volts.
"""
import math, statistics
import scope_analysis as SA

PASS, FAIL, MARGINAL, INFO = "PASS", "FAIL", "MARGINAL", "INFO"


def judge_min(name, value, limit, res, unit="s"):
    """value must be >= limit. res = the measurement's resolution (same unit)."""
    if value is None:
        return {"check": name, "result": INFO, "value": None, "limit": limit, "unit": unit, "note": "not observed in the captured records"}
    if limit is None:
        return {"check": name, "result": INFO, "value": value, "limit": None, "unit": unit}
    if value >= limit + res:
        r = PASS
    elif value < limit - res:
        r = FAIL
    else:
        r = MARGINAL
    return {"check": name, "result": r, "value": value, "limit": limit, "unit": unit,
            "note": "within the scope's resolution (%s) of the limit" % _f(res, unit) if r == MARGINAL else ""}


def judge_max(name, value, limit, res, unit):
    if value is None or limit is None:
        return judge_min(name, value, limit, res, unit)
    r = PASS if value <= limit - res else FAIL if value > limit + res else MARGINAL
    return {"check": name, "result": r, "value": value, "limit": limit, "unit": unit, "max": True,
            "note": "within the scope's resolution (%s) of the limit" % _f(res, unit) if r == MARGINAL else ""}


def judge_range(name, value, lo, hi, res, unit):
    if value is None:
        return judge_min(name, None, lo, res, unit)
    if lo + res <= value <= hi - res:
        r = PASS
    elif value < lo - res or value > hi + res:
        r = FAIL
    else:
        r = MARGINAL
    return {"check": name, "result": r, "value": value, "limit": (lo, hi), "unit": unit}


def _f(v, unit):
    if v is None:
        return "--"
    if unit == "s":
        return SA.fmt_t(v)
    if unit == "Hz":
        return SA.fmt_f(v)
    if unit in ("%", "deg"):
        return "%.1f %s" % (v, unit)
    return SA.fmt(v, unit)


def fmt_check(c):
    lim = c.get("limit")
    if isinstance(lim, tuple):
        ls = "%s..%s" % (_f(lim[0], c.get("unit")), _f(lim[1], c.get("unit")))
    elif lim is None:
        ls = ""
    else:
        ls = ("<= " if c.get("max") else ">= ") + _f(lim, c.get("unit"))
    s = "%-8s %-28s %s" % (c["result"], c["check"], _f(c.get("value"), c.get("unit")))
    if ls:
        s += "   (spec %s)" % ls
    if c.get("note"):
        s += "   " + c["note"]
    return s


def overall(checks):
    rs = [c["result"] for c in checks]
    return FAIL if FAIL in rs else MARGINAL if MARGINAL in rs else PASS if PASS in rs else INFO


# ------------------------------------------------------------------ step / dir
def step_dir_record(raw_step, raw_dir, rate, active="rising", p=None):
    """One record. Returns sample-domain lists: pulse widths (active level), spaces (inactive),
    step intervals, DIR setup (DIR change -> next active step edge) and hold (active step edge
    -> next DIR change)."""
    p = p or SA.Params()
    rises, falls, _ = SA.edges(raw_step, p.hyst, p.min_span)
    act, ina = (rises, falls) if active == "rising" else (falls, rises)
    widths = [next(b for b in ina if b > a) - a for a in act if any(b > a for b in ina)]
    spaces = [next(a for a in act if a > b) - b for b in ina if any(a > b for a in act)]
    ivals = [b - a for a, b in zip(act, act[1:])]
    out = {"steps": len(act), "widths": widths, "spaces": spaces, "intervals": ivals, "setup": [], "hold": [], "dir_changes": 0}
    if raw_dir is not None and max(raw_dir) - min(raw_dir) >= p.min_span:
        dr, df, _ = SA.edges(raw_dir, p.hyst, p.min_span)
        chg = sorted(dr + df)
        out["dir_changes"] = len(chg)
        for d in chg:
            nxt = [a for a in act if a > d]
            prv = [a for a in act if a < d]
            if nxt:
                out["setup"].append(nxt[0] - d)
            if prv:
                out["hold"].append(d - prv[-1])
    return out


def step_dir(pairs, rate, active="rising", spec=None, p=None):
    """pairs: [(raw_step, raw_dir|None), ...] from several records at the same rate.
    spec keys (seconds / Hz, any subset): min_pulse, min_space, min_setup, min_hold, max_rate.
    Minimums are taken over all records: that is what a drive sees as the worst case."""
    spec = spec or {}
    agg = {"steps": 0, "widths": [], "spaces": [], "intervals": [], "setup": [], "hold": [], "dir_changes": 0}
    for rs, rd in pairs:
        r = step_dir_record(rs, rd, rate, active, p)
        for k in agg:
            agg[k] = agg[k] + r[k] if isinstance(agg[k], list) else agg[k] + r[k]
    res = 1.0 / rate
    mn = lambda v: (min(v) / rate) if v else None
    med = lambda v: (statistics.median(v) / rate) if v else None
    m = {"records": len(pairs), "steps": agg["steps"], "dir_changes": agg["dir_changes"], "active": active,
         "pulse_min_s": mn(agg["widths"]), "pulse_median_s": med(agg["widths"]),
         "space_min_s": mn(agg["spaces"]), "space_median_s": med(agg["spaces"]),
         "setup_min_s": mn(agg["setup"]), "hold_min_s": mn(agg["hold"]),
         "rate_max_hz": (rate / min(agg["intervals"])) if agg["intervals"] else None,
         "rate_median_hz": (rate / statistics.median(agg["intervals"])) if agg["intervals"] else None,
         "resolution_s": res}
    have_dir = any(rd is not None for _, rd in pairs)
    checks = [judge_min("step pulse width (min)", m["pulse_min_s"], spec.get("min_pulse"), res),
              judge_min("step space (min)", m["space_min_s"], spec.get("min_space"), res)]
    if have_dir:
        checks += [judge_min("DIR setup before step (min)", m["setup_min_s"], spec.get("min_setup"), res),
                   judge_min("DIR hold after step (min)", m["hold_min_s"], spec.get("min_hold"), res)]
    rres = (m["rate_max_hz"] or 0) * res * (m["rate_max_hz"] or 0)          # d(1/T) = dT / T^2
    checks.append(judge_max("step rate (max)", m["rate_max_hz"], spec.get("max_rate"), rres, "Hz"))
    if agg["steps"] == 0:
        checks = [{"check": "step pulses", "result": FAIL, "value": None, "limit": None,
                   "note": "no step edges in %d record(s): axis not moving, wrong pin, or the timebase is too fast" % len(pairs)}]
    elif have_dir and agg["dir_changes"] == 0 and (spec.get("min_setup") or spec.get("min_hold")):
        checks.append({"check": "DIR changes", "result": INFO, "value": None, "limit": None,
                       "note": "no DIR change captured: jog back and forth (e.g. a short reversal) to measure setup/hold"})
    return {"metrics": m, "checks": checks, "verdict": overall(checks)}


# ------------------------------------------------------------------ encoder
def encoder(raw_a, raw_b, rate, lines=None, p=None):
    """Quadrature: phase of B relative to A (+90 = A leads), duty of each, the closest A-to-B
    edge spacing (as the counter sees it: below ~2 samples of the counter's filter the state
    machine can miss a count) and speed (lines = pulses per rev per channel)."""
    p = p or SA.Params()
    t = SA.timing(raw_a, raw_b, rate, p)
    pa, pb = SA.pulse_metrics(raw_a, rate, p, timing=False), SA.pulse_metrics(raw_b, rate, p, timing=False)
    ra, fa, _ = SA.edges(raw_a, p.hyst, p.min_span)
    rb, fb, _ = SA.edges(raw_b, p.hyst, p.min_span)
    ea, eb = sorted(ra + fa), sorted(rb + fb)
    gap = min((abs(x - y) for x in ea for y in eb), default=None)
    fA = SA.frequency(raw_a, rate, p)
    m = {"freq_hz": fA, "phase_deg": t.get("phase_deg") if t and t.get("same_freq") else None,
         "same_freq": bool(t and t.get("same_freq")), "duty_a": pa["duty"] if pa else None, "duty_b": pb["duty"] if pb else None,
         "min_edge_gap_s": gap / rate if gap is not None else None, "resolution_s": 1.0 / rate}
    if lines and fA:
        m["rpm"] = fA / lines * 60.0
    checks = []
    if not ea or not eb:
        checks.append({"check": "A and B both switching", "result": FAIL, "value": None, "limit": None,
                       "note": "%s has no edges: encoder not turning, wrong pin, or no supply to the encoder" % ("A" if not ea else "B")})
        return {"metrics": m, "checks": checks, "verdict": FAIL}
    if not m["same_freq"]:
        checks.append({"check": "A and B same frequency", "result": FAIL, "value": None, "limit": None,
                       "note": "A %s, B %s: a missing or noisy channel" % (SA.fmt_f(t["f1"]) if t else "?", SA.fmt_f(t["f2"]) if t else "?")})
    else:
        ph = abs(m["phase_deg"])
        res_deg = 360.0 * (1.0 / rate) * (fA or 0)
        checks.append(judge_range("phase |A-B|", ph, 60.0, 120.0, res_deg, "deg"))
        checks[-1]["note"] = ("A leads B" if m["phase_deg"] > 0 else "B leads A") + " (direction)"
    for nm in ("a", "b"):
        d = m["duty_" + nm]
        checks.append(judge_range("duty %s" % nm.upper(), d * 100 if d is not None else None, 35.0, 65.0, 100.0 / max(1, (rate / fA) if fA else 1), "%"))
    checks.append(judge_min("closest A/B edge spacing", m["min_edge_gap_s"], 2.0 / rate, 0.0))
    checks[-1]["note"] = "edges this close look simultaneous to a counter (illegal transition)" if checks[-1]["result"] == FAIL else ""
    return {"metrics": m, "checks": checks, "verdict": overall(checks)}


# ------------------------------------------------------------------ logic levels
FAMILIES = {                         # (VIL max, VIH min), volts
    "3v3": (0.8, 2.0),               # LVTTL / LVCMOS 3.3 V
    "5v-ttl": (0.8, 2.0),
    "5v-cmos": (1.5, 3.5),
    "24v": (5.0, 15.0),              # IEC 61131-2 type 1 sinking input (typical 24 V field I/O)
}


def logic_levels(raw, vpc, zero, vil, vih, guard=3, p=None):
    """Plateau levels plus the WORST sample on each plateau (edges +/- `guard` samples excluded):
    noise or ground bounce that dips a high below VIH, or lifts a low above VIL, is what makes an
    input double-count or chatter."""
    p = p or SA.Params()
    v = lambda c: (c - 128 - zero) * vpc
    top_c, base_c = SA.plateau_levels(raw)
    flat = (max(raw) - min(raw)) < p.min_span
    rises, falls, mid = SA.edges(raw, p.hyst, p.min_span)
    near = set()
    for e in rises + falls:
        for k in range(int(e) - guard, int(e) + guard + 2):
            near.add(k)
    # plateau membership from the edge sequence (hysteresis), not from each sample's own value:
    # a dip inside a high plateau must count as a bad HIGH sample, not as a low one
    ev = sorted([(e, True) for e in rises] + [(e, False) for e in falls])
    state, k, hi, lo = (raw[0] > mid) if mid is not None else False, 0, [], []
    for i, x in enumerate(raw):
        while k < len(ev) and ev[k][0] <= i:
            state = ev[k][1]; k += 1
        if i not in near:
            (hi if state else lo).append(x)
    m = {"top_v": v(top_c), "base_v": v(base_c), "resolution_v": vpc, "flat": flat}
    checks = []
    if flat:
        lvl = v(sum(raw) / len(raw))
        m["level_v"] = lvl
        state = "HIGH" if lvl >= vih else "LOW" if lvl <= vil else "UNDEFINED (between VIL and VIH)"
        checks.append({"check": "static level", "result": FAIL if state.startswith("UNDEF") else PASS,
                       "value": lvl, "limit": (vil, vih), "unit": "V", "note": "reads as " + state})
        return {"metrics": m, "checks": checks, "verdict": overall(checks)}
    m["high_worst_v"] = v(min(hi)) if hi else None
    m["low_worst_v"] = v(max(lo)) if lo else None
    checks.append(judge_min("high level (plateau)", m["top_v"], vih, vpc, "V"))
    checks.append(judge_max("low level (plateau)", m["base_v"], vil, vpc, "V"))
    checks.append(judge_min("high level (worst sample)", m["high_worst_v"], vih, vpc, "V"))
    checks.append(judge_max("low level (worst sample)", m["low_worst_v"], vil, vpc, "V"))
    if m["high_worst_v"] is not None:
        m["noise_margin_high_v"] = m["high_worst_v"] - vih
    if m["low_worst_v"] is not None:
        m["noise_margin_low_v"] = vil - m["low_worst_v"]
    return {"metrics": m, "checks": checks, "verdict": overall(checks)}


# ------------------------------------------------------------------ supply rail
# The scope's own peak-to-peak noise in ADC counts on a quiet input (CH1 on the Pi's driven pin,
# 12 frames per range at 50 kS/s and 5 MS/s, 2026-10-12): typical 2-4, worst 3-7 on ranges 6-9;
# range 5 up to 6, range 4 up to 14. Ripple at or below this is not distinguishable from the scope.
NOISE_PP_COUNTS = {4: 14, 5: 6, 6: 7, 7: 4, 8: 3, 9: 3}


def _smooth_min(raw, k=8):
    """Lowest k-sample average: a real sag/dropout lasts many samples, ADC noise does not."""
    if len(raw) < k:
        return min(raw)
    s = sum(raw[:k]); lo = s
    for i in range(k, len(raw)):
        s += raw[i] - raw[i - k]; lo = min(lo, s)
    return lo / float(k)


def rail(recs, nominal=None, tol=0.05, ripple_max=None):
    """recs: [{"raw","rate","vpc","zero","rng"}...] DC-coupled. Level = mean of each record; sag =
    the lowest 8-sample average anywhere; ripple = peak-to-peak within a record (median and
    worst) and its main frequency, judged with the scope's own noise floor (NOISE_PP_COUNTS)
    taken into account. ripple_max defaults to 2 % of nominal."""
    means, pps, lows, highs = [], [], [], []
    for r in recs:
        lv = SA.levels(r["raw"], r["vpc"], r.get("zero", 0.0))
        means.append(lv["vavg"]); pps.append(lv["vpp"]); highs.append(lv["vmax"])
        lows.append((_smooth_min(r["raw"]) - 128 - r.get("zero", 0.0)) * r["vpc"])
    last = recs[-1]
    vpc = last["vpc"]
    floor_v = NOISE_PP_COUNTS.get(last.get("rng"), 7) * vpc
    pk = SA.spectrum_peaks(last["raw"], last["rate"], 1) if max(last["raw"]) - min(last["raw"]) > NOISE_PP_COUNTS.get(last.get("rng"), 7) else []
    m = {"frames": len(recs), "mean_v": statistics.fmean(means), "lowest_frame_mean_v": min(means), "highest_frame_mean_v": max(means),
         "lowest_avg8_v": min(lows), "max_sample_v": max(highs), "ripple_pp_median_v": statistics.median(pps),
         "ripple_pp_worst_v": max(pps), "ripple_freq_hz": pk[0]["hz"] if pk else None, "resolution_v": vpc,
         "scope_noise_pp_v": floor_v}
    checks = []
    if nominal:
        lo, hi = nominal * (1 - tol), nominal * (1 + tol)
        checks.append(judge_range("level (mean)", m["mean_v"], lo, hi, vpc, "V"))
        checks.append(judge_min("lowest 8-sample average (sag)", m["lowest_avg8_v"], lo, vpc, "V"))
        rmax = ripple_max if ripple_max is not None else 0.02 * nominal
        w = m["ripple_pp_worst_v"]
        c = {"check": "ripple p-p (worst frame)", "value": w, "limit": rmax, "unit": "V", "max": True}
        floor_v *= 1 - 1e-9                                    # float slack: a limit equal to the floor passes
        if w <= floor_v:
            c["result"] = PASS if rmax >= floor_v else MARGINAL
            c["note"] = "at the scope's own noise floor (%s on this range)%s" % (SA.fmt(floor_v, "V"), "" if rmax >= floor_v else
                                                                                   ": the limit is below what this range can show")
        else:
            c["result"] = PASS if w <= rmax else FAIL if w > rmax + floor_v else MARGINAL
            c["note"] = "scope noise floor %s" % SA.fmt(floor_v, "V")
        checks.append(c)
    return {"metrics": m, "checks": checks, "verdict": overall(checks) if checks else INFO}


# ------------------------------------------------------------------ contact bounce
def unrotate_triggered(raw, rising=True, pre=512, p=None):
    """A triggered record (NORMAL/SINGLE) is one continuous segment rotated so the trigger sits
    `pre` samples after the seam (OpenScope fpga.c; the firmware's own un-rotation is compiled
    out). For an isolated event (a switch press) the trigger is the FIRST edge: pick the edge in
    the trigger direction whose rotation leaves no edge at all before it. Returns
    (time-ordered raw, seam index) or (raw, None) when no candidate is consistent."""
    p = p or SA.Params()
    n = len(raw)
    rises, falls, _ = SA.edges(raw, p.hyst, p.min_span)
    best = None
    alle = sorted(rises + falls)
    for e in (rises if rising else falls):
        sm = (int(round(e)) - pre) % n
        # the trigger sits ~1-3 samples off `pre`; when the seam is a visible step, snap to it
        near = [x for x in alle if min(abs(x - sm), n - abs(x - sm)) <= 6]
        if near:
            sm = int(math.ceil(min(near, key=lambda x: min(abs(x - sm), n - abs(x - sm))))) % n
        u = bytes(raw[sm:]) + bytes(raw[:sm])
        r2, f2, _ = SA.edges(u, p.hyst, p.min_span)
        before = sum(1 for x in r2 + f2 if 2 <= x < pre - 6)
        jump = abs(raw[sm] - raw[sm - 1])
        if before == 0 and (best is None or jump > best[1]):
            best = (sm, jump, u)
    return (best[2], best[0]) if best else (bytes(raw), None)


def bounce(raw, rate, debounce=None, p=None):
    """Transitions in one triggered record: the bounce lasts from the first to the last
    transition; the final level is where the contact settled. debounce = the input filter's
    time (e.g. LinuxCNC `debounce` delay x servo period, or the card's input filter): the
    bounce must be shorter or the input counts extra edges."""
    p = p or SA.Params()
    rises, falls, mid = SA.edges(raw, p.hyst, p.min_span)
    ev = sorted([(r, "rise") for r in rises] + [(f, "fall") for f in falls])
    m = {"transitions": len(ev), "resolution_s": 1.0 / rate, "record_s": len(raw) / rate}
    if not ev:
        return {"metrics": m, "checks": [], "verdict": INFO}
    m["first_s"] = ev[0][0] / rate
    m["bounce_s"] = (ev[-1][0] - ev[0][0]) / rate
    m["final"] = "high" if ev[-1][1] == "rise" else "low"
    m["extra_transitions"] = len(ev) - 1
    gaps = [b[0] - a[0] for a, b in zip(ev, ev[1:])]
    m["shortest_pulse_s"] = min(gaps) / rate if gaps else None
    m["settled_margin_s"] = (len(raw) - ev[-1][0]) / rate      # quiet time after the last transition
    checks = []
    if debounce:
        checks.append(judge_max("bounce duration", m["bounce_s"], debounce, 1.0 / rate, "s"))
        if m["settled_margin_s"] < 0.2 * m["record_s"]:
            checks.append({"check": "record covers the bounce", "result": MARGINAL, "value": m["settled_margin_s"], "limit": None, "unit": "s",
                           "note": "last transition near the end of the record: it may continue past it; use a slower timebase"})
    return {"metrics": m, "checks": checks, "verdict": overall(checks) if checks else INFO}
