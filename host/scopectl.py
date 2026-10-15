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
    .venv/bin/python host/scopectl.py cnc step|encoder|levels|rail|bounce ...   # PASS/FAIL bring-up checks (CNC_BRINGUP.md)

Everything goes through http://scope-pi.local:8080 (override with --url or SCOPE_URL).
`describe` prints a plain-language summary plus flags (clipped, no signal, low resolution,
uncalibrated range, intermittent, mains pickup) and can render a PNG of the record with
real volt/time axes (matplotlib on this machine, nothing needed on the Pi).
"""
import argparse, base64, json, math, os, re, statistics, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_analysis as SA
import cnc_checks as CC
from scope_analysis import fmt, fmt_f, fmt_t, fmt_rate, Params

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


def ch_fields(f, ch):
    decode(f)
    h = f["hdr"]
    return {"raw": f["b%d" % ch], "vpc": f["vpc%d" % ch], "rng": h["rng%d" % ch], "en": h["en%d" % ch],
            "cpl": CPL[h["cpl%d" % ch]], "probe": 10 if h["prb%d" % ch] else 1, "meas": (f.get("meas") or {}).get("ch%d" % ch, {})}


# ------------------------------------------------------------------ analysis (shared module)
def rec_from_frame(f, ch, info):
    """One channel of one viewer frame -> a scope_analysis record."""
    c = ch_fields(f, ch)
    v = f.get("valid")
    raw = c["raw"][:v] if v else SA.clean_record(c["raw"])        # continuous part only
    return {"raw": raw, "clean": True, "rate": f["rate"], "vpc": c["vpc"], "zero": f.get("zero%d" % ch, 0.0) or 0.0,
            "probe": c["probe"], "cpl": c["cpl"], "rng": c["rng"], "tier": info["range_tier"][c["rng"]],
            "enabled": bool(c["en"]), "channel": ch, "counts_per_div": info["counts_per_div"], "unit": f.get("unit%d" % ch, "V"),
            "gain": info.get("calib", {}).get("gain", {}).get(str(ch), {}).get(str(c["rng"]), 1.0), "t": f.get("t")}


def analyze(fs, ch, info, p=None):
    return SA.analyze_records([rec_from_frame(f, ch, info) for f in fs], p)


import re as _re


def SA_parse_where(expr):
    conds = []
    for part in (expr or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _re.match(r"^(ch[12])_(amp|mean|rms|freq|clipped|glitches|duty|span)\s*(<=|>=|==|!=|<|>)\s*(-?[\d.eE+-]+)$", part)
        if not m:
            raise SystemExit("bad condition %r (e.g. ch2_mean<4.5, ch1_glitches>0)" % part)
        conds.append((m.group(1), m.group(2), m.group(3), float(m.group(4))))
    return conds


def SA_row_matches(row, conds):
    for ch, field, op, val in conds:
        x = (row.get(ch) or {}).get(field)
        if x is None:
            return False
        x = float(x)
        if not {"<": x < val, "<=": x <= val, ">": x > val, ">=": x >= val, "==": x == val, "!=": x != val}[op]:
            return False
    return True


def parse_params(items):
    kw = {}
    for it in items or []:
        k, v = it.split("=", 1)
        kw[k] = float(v) if k != "mains_band" else tuple(float(x) for x in v.split(","))
    return Params(**kw)


def parse_time(t):
    """unix seconds | 'now' | '-10m' / '-2h' / '-30s' (ago) | 'YYYY-MM-DD HH:MM[:SS]' local |
    'mark:<text>' (latest marker whose text contains <text>), optionally '+30s' / '-1m' after it."""
    if t is None or t == "now":
        return time.time()
    t = str(t).strip()
    if t.startswith("mark:"):
        body, off = t[5:], 0.0
        for sep in ("+", "-"):
            if sep in body[1:]:
                k = body.rindex(sep)
                tail = body[k + 1:]
                if tail and tail[-1] in "smh" and tail[:-1].replace(".", "").isdigit():
                    off = float(tail[:-1]) * {"s": 1, "m": 60, "h": 3600}[tail[-1]] * (1 if sep == "+" else -1)
                    body = body[:k]
                break
        ms = [m for m in api("/api/marks") if body.lower() in m["text"].lower()]
        if not ms:
            raise SystemExit("no marker matching %r" % body)
        return ms[-1]["t"] + off
    if t.startswith("-") and t[-1] in "smhd":
        return time.time() - float(t[1:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[t[-1]]
    try:
        return float(t)
    except ValueError:
        pass
    for fmt_ in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%H:%M:%S", "%H:%M"):
        try:
            st = time.strptime(t, fmt_)
            if fmt_.startswith("%H"):
                now = time.localtime()
                st = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, st.tm_hour, st.tm_min, st.tm_sec, 0, 0, -1))
            return time.mktime(st)
        except ValueError:
            continue
    raise SystemExit("cannot parse time %r" % t)


# ------------------------------------------------------------------ autoset
def autoset(ch, info, verbose=True):
    """Deterministic: find the finest range that holds the signal, a timebase that shows
    ~10 cycles, and a mid-level trigger. Returns the final description."""
    say = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)
    d = autorange(ch, info, say)
    return _autoset_time(ch, info, d, say)


def autorange(ch, info, say=None):
    """Vertical only: the finest range that holds the signal's top and base."""
    say = say or (lambda *a: print(*a, file=sys.stderr))
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
    return d


def _autoset_time(ch, info, d, say):
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
        # prefer measured codes; a provisional one only if no measured code is within 3x
        def dist(t):
            return abs(math.log(t["rate"] / want_rate)) + (0.0 if t["tier"].startswith("measured") else math.log(3.0))
        t = min(tbs, key=dist)
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


# ------------------------------------------------------------------ two-channel timing (shared module)
def timing(fs, info, p=None):
    f = fs[-1]
    return SA.timing(f["b1"], f["b2"], f["rate"], p)


# ------------------------------------------------------------------ CNC bring-up checks (host/cnc_checks.py)
def si(x):
    """'2.5us' '5ms' '100k' '24' '0.5V' -> float (seconds / Hz / volts)."""
    if x is None:
        return None
    m = re.match(r"^\s*([-+\d.eE]+)\s*([munpkM]?)", str(x))
    if not m:
        raise SystemExit("bad value %r" % x)
    return float(m.group(1)) * {"": 1, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "k": 1e3, "M": 1e6}[m.group(2)]


# A typical microstepping driver's input timing (DM542-class); the drive's own datasheet wins.
STEP_SPEC_DEFAULT = {"min_pulse": 2.5e-6, "min_space": 2.5e-6, "min_setup": 5e-6, "min_hold": 5e-6}


def _tb_codes(info, measured_only=True):
    return sorted((t for t in info["timebases"] if t["tier"].startswith("measured") or not measured_only), key=lambda t: -t["rate"])


def _set_tb(code):
    cmd("fpga scope timebase 0x%02X" % code)


def _recs(fs, ch, info):
    return [rec_from_frame(f, ch, info) for f in fs]


def _pairs(fs, info):
    out = []
    for f in fs:
        r1, r2 = rec_from_frame(f, 1, info), rec_from_frame(f, 2, info)
        out.append((r1["raw"], r2["raw"], r1["rate"]))
    return out


def _print_result(title, r, extra=None):
    print("== %s: %s" % (title, r["verdict"]))
    for c in r["checks"]:
        print("  " + CC.fmt_check(c))
    for k, v in (extra or {}).items():
        print("  %-28s %s" % (k, v))


def _tb_for_plateaus(ch, info, min_samples=20):
    """Timebase for level checks: the slowest measured rate at which the SHORTER plateau
    (high or low time) still spans `min_samples`, so plateau noise is seen, not just edges."""
    for t in _tb_codes(info):                                   # fastest first: find complete pulses
        _set_tb(t["code"])
        pm = analyze(frames(2, settle=0.5), ch, info)["pulse"]
        if pm and pm["high_s"] and pm["low_s"]:
            need = min_samples / min(pm["high_s"], pm["low_s"])
            ok = [c for c in _tb_codes(info) if c["rate"] >= need]
            best = ok[-1] if ok else _tb_codes(info)[0]
            _set_tb(best["code"])
            print("levels: timebase 0x%02X (%s): shortest plateau %s = %.0f samples"
                  % (best["code"], fmt_rate(best["rate"]), fmt_t(min(pm["high_s"], pm["low_s"])), min(pm["high_s"], pm["low_s"]) * best["rate"]), file=sys.stderr)
            return best
    return None


def cnc(a, info):
    p = Params()
    f0 = frames(1)[-1]
    restore = {"tb": f0["hdr"]["tb"], "tmode": TMODE[f0["hdr"]["tmode"]]}
    try:
        if a.check == "step":
            spec = dict(STEP_SPEC_DEFAULT) if not a.no_default_spec else {}
            for k in ("min_pulse", "min_space", "min_setup", "min_hold", "max_rate"):
                if getattr(a, k) is not None:
                    spec[k] = si(getattr(a, k))
            if not a.no_autoset:
                autorange(1, info)
                if a.dir_ch:
                    autorange(2, info)
            # finest measured rate that still holds complete pulses; slower until enough are seen
            res = None
            for t in _tb_codes(info):
                if t["rate"] > 5.1e6 and not a.provisional:
                    continue
                _set_tb(t["code"])
                fs = frames(a.frames, settle=0.6)
                pr = _pairs(fs, info)
                res = CC.step_dir([(x[0], x[1] if a.dir_ch else None) for x in pr], pr[-1][2], a.active, spec, p)
                if res["metrics"]["steps"] >= 3 and res["metrics"]["pulse_min_s"] is not None:
                    break
                print("  (%s: %d complete step pulses, trying a slower timebase)" % (fmt_rate(t["rate"]), res["metrics"]["steps"]), file=sys.stderr)
            m = res["metrics"]
            extra = {"records": "%d at %s (resolution %s)" % (m["records"], fmt_rate(1 / m["resolution_s"]), fmt_t(m["resolution_s"])),
                     "steps seen / DIR changes": "%d / %d" % (m["steps"], m["dir_changes"]),
                     "pulse median / space median": "%s / %s" % (fmt_t(m["pulse_median_s"]) if m["pulse_median_s"] else "--", fmt_t(m["space_median_s"]) if m["space_median_s"] else "--"),
                     "step rate median": fmt_f(m["rate_median_hz"]) if m["rate_median_hz"] else "--"}
            if a.steps_per_mm and m["rate_median_hz"]:
                extra["feed (median / max)"] = "%.0f / %.0f mm/min" % (m["rate_median_hz"] / a.steps_per_mm * 60, m["rate_max_hz"] / a.steps_per_mm * 60)
            _print_result("step/dir (CH1 step%s, active %s)" % (", CH2 dir" if a.dir_ch else "", a.active), res, extra)
            out = res

        elif a.check == "encoder":
            if not a.no_autoset:
                autoset(1, info, verbose=True); autorange(2, info)
            fs = frames(a.frames, settle=0.6)
            per = [CC.encoder(r1, r2, rate, a.lines, p) for r1, r2, rate in _pairs(fs, info)]
            order = {CC.FAIL: 0, CC.MARGINAL: 1, CC.PASS: 2, CC.INFO: 3}
            worst = min(per, key=lambda r: order[r["verdict"]])
            ph = [r["metrics"]["phase_deg"] for r in per if r["metrics"]["phase_deg"] is not None]
            gaps = [r["metrics"]["min_edge_gap_s"] for r in per if r["metrics"]["min_edge_gap_s"] is not None]
            extra = {"frames": "%d (%d %s, worst shown)" % (len(per), sum(r["verdict"] == CC.PASS for r in per), "PASS")}
            if ph:
                extra["phase over frames"] = "%.0f .. %.0f deg" % (min(ph), max(ph))
            if gaps:
                extra["closest A/B edges (all)"] = fmt_t(min(gaps))
            if worst["metrics"].get("freq_hz"):
                extra["A frequency"] = fmt_f(worst["metrics"]["freq_hz"])
            if worst["metrics"].get("rpm") is not None:
                extra["speed"] = "%.1f rpm (%g lines/rev)" % (worst["metrics"]["rpm"], a.lines)
            _print_result("encoder (CH1 A, CH2 B)", worst, extra)
            out = {"worst": worst, "frames": per}

        elif a.check == "levels":
            vil, vih = (si(a.vil), si(a.vih)) if a.vil and a.vih else CC.FAMILIES[a.family]
            if not a.no_autoset:
                d = autorange(a.ch, info)
                if d["span_counts"] >= p.min_span:
                    _tb_for_plateaus(a.ch, info)
            fs = frames(a.frames, settle=0.6)
            recs = _recs(fs, a.ch, info)
            if not recs[-1]["vpc"]:
                raise SystemExit("range %d is uncalibrated: volts unknown" % recs[-1]["rng"])
            per = [CC.logic_levels(r["raw"], r["vpc"], r.get("zero", 0.0), vil, vih) for r in recs]
            order = {CC.FAIL: 0, CC.MARGINAL: 1, CC.PASS: 2, CC.INFO: 3}
            worst = min(per, key=lambda r: order[r["verdict"]])
            m = worst["metrics"]
            extra = {"thresholds": "VIL %s / VIH %s (%s)" % (fmt(vil, "V"), fmt(vih, "V"), "custom" if a.vil else a.family),
                     "frames": "%d, worst shown" % len(per)}
            if m.get("noise_margin_high_v") is not None:
                extra["noise margin high / low"] = "%s / %s" % (fmt(m["noise_margin_high_v"], "V"), fmt(m["noise_margin_low_v"], "V"))
            _print_result("logic levels CH%d" % a.ch, worst, extra)
            out = {"worst": worst, "frames": per}

        elif a.check == "rail":
            if not a.no_autoset:
                autorange(a.ch, info)
            _set_tb(int(a.tb, 0))
            n = max(3, int(a.seconds * 14))
            fs = frames(n, settle=0.6)
            recs = [{"raw": r["raw"], "rate": r["rate"], "vpc": r["vpc"], "zero": r.get("zero", 0.0), "rng": r["rng"]} for r in _recs(fs, a.ch, info)]
            if not recs[-1]["vpc"]:
                raise SystemExit("range uncalibrated: volts unknown")
            res = CC.rail(recs, si(a.nominal) if a.nominal else None, a.tol / 100.0, si(a.ripple) if a.ripple else None)
            m = res["metrics"]
            extra = {"frames": "%d over ~%.0f s at %s" % (m["frames"], a.seconds, fmt_rate(recs[-1]["rate"])),
                     "mean / lowest frame / highest frame": "%s / %s / %s" % (fmt(m["mean_v"], "V"), fmt(m["lowest_frame_mean_v"], "V"), fmt(m["highest_frame_mean_v"], "V")),
                     "ripple p-p median / worst": "%s / %s" % (fmt(m["ripple_pp_median_v"], "V"), fmt(m["ripple_pp_worst_v"], "V")),
                     "ripple main frequency": fmt_f(m["ripple_freq_hz"]) if m["ripple_freq_hz"] else "-- (nothing above the scope noise)",
                     "resolution": fmt(m["resolution_v"], "V") + " per ADC step"}
            _print_result("rail CH%d" % a.ch, res, extra)
            out = res

        elif a.check == "bounce":
            if f0["hdr"].get("tsrc", 0) != 0:
                raise SystemExit("the scope's trigger source is CH2; bounce uses CH1 (set the source on the device)")
            # idle level decides the edge and the range
            d = autorange(1, info)
            idle = d["mean"]
            vhigh = si(a.vhigh) if a.vhigh else None
            edge = a.edge or ("falling" if idle > 1.0 else "rising")
            if edge == "rising" and not vhigh and idle <= 1.0:
                raise SystemExit("line idles low: give --vhigh (the closed-contact voltage, e.g. 24) so the range and trigger level can be set")
            hi_v = max(idle, vhigh or 0)
            mv = info["mv_per_count"][str(1)] if str(1) in info["mv_per_count"] else info["mv_per_count"][0]
            cpd = info["counts_per_div"]
            rngs = [r for r in range(10) if mv[r] and hi_v <= 3.6 * mv[r] / 1000 * cpd]
            if rngs:
                cmd("fpga scope vdiv 1 %d" % min(rngs, key=lambda r: mv[r]))
            fs = frames(2, settle=0.6)
            r0 = rec_from_frame(fs[-1], 1, info)
            thr_v = hi_v / 2.0
            thr_c = 128 + r0.get("zero", 0.0) + thr_v / r0["vpc"]
            level = max(-100, min(100, int(round((thr_c - 128) * 216 / 256))))
            win = si(a.window)
            t = min(_tb_codes(info), key=lambda t: abs(math.log(1024 / t["rate"] / win)))
            _set_tb(t["code"])
            cmd("fpga scope edge " + edge); cmd("fpga scope level %d" % level); cmd("fpga scope trigmode normal")
            time.sleep(0.5)
            g0 = frames(1)[-1]["hdr"]["gen"]
            print("armed: CH1 %s edge at %s, record %s (%s). Operate the switch now (waiting up to %.0f s) ..."
                  % (edge, fmt(thr_v, "V"), fmt_t(1024 / t["rate"]), fmt_rate(t["rate"]), a.timeout))
            t0, got = time.time(), None
            while time.time() - t0 < a.timeout:
                f = frames(1, settle=0.05)[-1]
                if f["hdr"]["gen"] != g0:
                    time.sleep(0.3)                       # let the record finish filling
                    got = frames(1, settle=0.05)[-1]; break
            if not got:
                raise SystemExit("no trigger within %.0f s" % a.timeout)
            raw = got["b1"]
            k = got.get("seam") or 0                             # undo the hub's free-run junk rotation, if it applied one
            if k:
                raw = raw[len(raw) - k:] + raw[:len(raw) - k]
            u, seam = CC.unrotate_triggered(raw, edge == "rising", 512, p)
            if seam is None:
                print("  note: could not place the trigger in this record (edges before it?); analysing it as read", file=sys.stderr)
            got = dict(got, b1=u, valid=len(u))                 # time-ordered, trigger at 512
            r = rec_from_frame(got, 1, info)
            res = CC.bounce(r["raw"], r["rate"], si(a.debounce) if a.debounce else None, p)
            m = res["metrics"]
            extra = {"transitions": m["transitions"]}
            if m["transitions"]:
                extra.update({"bounce (first..last edge)": fmt_t(m["bounce_s"]) if m["bounce_s"] else "none (one clean edge)",
                              "shortest pulse": fmt_t(m["shortest_pulse_s"]) if m.get("shortest_pulse_s") else "--",
                              "settled": m["final"], "record": fmt_t(m["record_s"])})
            _print_result("contact bounce CH1", res, extra)
            out = res
            if a.png:
                plot([got], [1], info, a.png, {1: analyze([got], 1, info)}); print("plot:", a.png)
        if a.json:
            print(json.dumps(out, default=str, indent=1))
        return out
    finally:                                   # leave the trigger mode and timebase as they were
        try:
            cmd("fpga scope trigmode " + restore["tmode"]); _set_tb(restore["tb"])
        except SystemExit:
            pass


# ------------------------------------------------------------------ self-test
REF_V = 3.194      # Pi GPIO high level (the owner's meter, 2026-10-10)


def refsig(mode, hz=1000.0, duty=0.5):
    return api("/api/refsig", {"mode": mode, "hz": hz, "duty": duty})


def selftest(info, verbose=True):
    """End-to-end check of scope + Pi + analysis against the Pi's own reference on GPIO18.
    Needs a probe tip on header pin 12 and its clip on pin 6 (either channel). Restores settings."""
    say = (lambda *x: print(*x)) if verbose else (lambda *x: None)
    res = []

    def check(name, ok, detail):
        res.append((name, bool(ok), detail))
        say("%s %-28s %s" % ("PASS" if ok else "FAIL", name, detail))

    h0 = api("/api/frame.json")["hdr"]
    try:
        cmd("fpga scope trigmode auto")
        cmd("fpga scope timebase 0x0D")
        for ch in (1, 2):
            cmd("fpga scope vdiv %d 6" % ch)
        # 1. which channel carries the reference?
        lv = {}
        for mode in ("high", "low"):
            refsig(mode)
            fs = frames(4, settle=0.8)
            lv[mode] = {ch: statistics.fmean(SA.levels(f["b%d" % ch], f["vpc%d" % ch], f.get("zero%d" % ch, 0.0))["vavg"] for f in fs) for ch in (1, 2)}
        chans = [ch for ch in (1, 2) if lv["high"][ch] - lv["low"][ch] > 2.0]
        check("reference detected", chans, "on CH%s (high-low = %s)" % ("+".join(map(str, chans)) or "none",
              ", ".join("CH%d %.2f V" % (ch, lv["high"][ch] - lv["low"][ch]) for ch in (1, 2))))
        if not chans:
            say("put a probe tip on Pi header pin 12 (GPIO18) and its ground clip on pin 6, then re-run")
            return res
        for ch in chans:
            pre = "CH%d " % ch
            check(pre + "zero (pin low)", abs(lv["low"][ch]) < 0.03, "%+.1f mV (limit +/-30)" % (lv["low"][ch] * 1e3))
            e = (lv["high"][ch] / REF_V - 1) * 100
            check(pre + "DC level (pin high)", abs(e) < 1.5, "%.3f V vs %.3f V (%+.2f%%, limit 1.5)" % (lv["high"][ch], REF_V, e))
            # 1 kHz square at 125 kS/s
            refsig("pwm", 1000)
            cmd("fpga scope timebase 0x0D")
            d = analyze(frames(5, settle=0.9), ch, info)
            fe = ((d["freq_hz"] or 0) / 1000.0 - 1) * 100
            check(pre + "1 kHz frequency", d["freq_hz"] and abs(fe) < 0.2, "%s (%+.3f%%, limit 0.2)" % (fmt_f(d["freq_hz"]), fe))
            ae = (d["amp_top_base"] / REF_V - 1) * 100
            check(pre + "1 kHz amplitude", abs(ae) < 1.5, "%.3f V (%+.2f%%, limit 1.5)" % (d["amp_top_base"], ae))
            du = d["duty"] or 0
            check(pre + "1 kHz duty", abs(du - 0.5) < 0.01, "%.1f%% (limit 49-51)" % (du * 100))
            check(pre + "shape / glitches", d["shape"] == "square/pulse" and not (d["pulse"] or {}).get("glitches"),
                  "%s, %s glitches" % (d["shape"], (d["pulse"] or {}).get("glitches")))
            pk = (d.get("spectrum_peaks") or [{}])[0].get("hz")
            check(pre + "spectrum peak", pk and abs(pk / 1000 - 1) < 0.01, "%s" % fmt_f(pk))
            other = 2 if ch == 1 else 1
            f = frames(1)[-1]
            st = SA.decode_steps(f["b%d" % ch], f["b%d" % other], f["rate"])
            sr = st.get("step_rate_hz") or 0
            check(pre + "step decoder", sr and abs(sr / 1000 - 1) < 0.003, "%d steps, %s" % (st.get("steps", 0), fmt_f(sr) if sr else "--"))
            # 10 kHz, 25 % duty at 2.5 MS/s (0.4 us/sample): pulse width, limit = max(2 %, 1 sample)
            refsig("pwm", 10000, 0.25)
            cmd("fpga scope timebase 0x09")
            d = analyze(frames(5, settle=0.9), ch, info)
            hs = (d["pulse"] or {}).get("high_s") or 0
            lim = max(0.02, (1.0 / (d["rate_sps"] or 1)) / 25e-6)
            check(pre + "pulse width 25 us", abs(hs / 25e-6 - 1) < lim, "%s (limit %.1f%%), duty %.1f%%" % (fmt_t(hs), lim * 100, (d["duty"] or 0) * 100))
            # 100 kHz at 5 MS/s
            refsig("pwm", 100000)
            cmd("fpga scope timebase 0x08")
            d = analyze(frames(5, settle=0.9), ch, info)
            fe = ((d["freq_hz"] or 0) / 1e5 - 1) * 100
            check(pre + "100 kHz @ 5 MS/s", d["freq_hz"] and abs(fe) < 0.3, "%s (%+.3f%%, limit 0.3)" % (fmt_f(d["freq_hz"]), fe))
        # 2. the Pi side
        h1 = api("/api/health"); time.sleep(2.5); h2 = api("/api/health")
        grow = (h2["recorder"]["frames_total"] or 0) - (h1["recorder"]["frames_total"] or 0)
        check("recorder writing", grow > 0, "+%d frames in 2.5 s" % grow)
        check("metrics index", (h2["recorder"]["indexer_backlog"] or 0) < 100 and not h2["recorder"]["indexer_dropped"],
              "backlog %s, dropped %s" % (h2["recorder"]["indexer_backlog"], h2["recorder"]["indexer_dropped"]))
        import urllib.parse
        ev = api("/api/events?from=%f&to=%f&where=%s" % (time.time() - 60, time.time(), urllib.parse.quote("ch%d_amp>2" % chans[0])), timeout=60)
        check("event search", ev["matched"] > 0, "%d frames matched ch%d_amp>2 in the last minute" % (ev["matched"], chans[0]))
        check("disk", h2["disk_free_gb"] > 5, "%.1f GB free" % h2["disk_free_gb"])
    finally:
        refsig("pwm", 1000)
        cmd("fpga scope timebase 0x%02X" % h0["tb"])
        cmd("fpga scope vdiv 1 %d" % h0["rng1"]); cmd("fpga scope vdiv 2 %d" % h0["rng2"])
        cmd("fpga scope trigmode %s" % TMODE[h0["tmode"]])
    n_fail = sum(1 for _, ok, _ in res if not ok)
    say("SELFTEST %s: %d/%d passed" % ("PASSED" if not n_fail else "FAILED", len(res) - n_fail, len(res)))
    return res


# ------------------------------------------------------------------ plotting
def plot(fs, chans, info, path, descs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    f = fs[-1]
    rate = f["rate"] or 0
    n = len(f["b1"])
    t0 = -512 if f["hdr"].get("ordered") else 0
    xs = [((i + t0) / rate) for i in range(n)] if rate else list(range(n))
    xl = "time (s, 0 = trigger)" if (rate and f["hdr"].get("ordered")) else ("time (s)" if rate else "sample")
    fig, axes = plt.subplots(len(chans), 1, figsize=(11, 3.2 * len(chans)), sharex=True, squeeze=False)
    for ax, ch in zip(axes[:, 0], chans):
        c = ch_fields(f, ch)
        z = f.get("zero%d" % ch, 0.0) or 0.0
        vals = [(b - 128 - z) * c["vpc"] for b in c["raw"]] if c["vpc"] else list(c["raw"])
        v = f.get("valid") or len(vals)
        ax.plot(xs[:v], vals[:v], color="#1fb4d8" if ch == 1 else "#2458e8", lw=1)
        if v < len(vals):
            ax.plot(xs[v:], vals[v:], color="#bbbbbb", lw=1)          # junk block of the ring buffer, not signal
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
    p = sub.add_parser("la", help="8-channel logic analyzer on the Pi (sigrok): status|capture|list|stats|decode|fetch")
    p.add_argument("action", choices=["status", "capture", "list", "stats", "decode", "fetch"])
    p.add_argument("name", nargs="?", help="capture name (stats/decode/fetch; default: latest)")
    p.add_argument("--rate", default="4M"); p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--demo", action="store_true", help="sigrok's fake device (no hardware)")
    p.add_argument("--label", default=""); p.add_argument("--trigger", help="sigrok trigger, e.g. D0=r")
    p.add_argument("--step"); p.add_argument("--dir", default="D1"); p.add_argument("--dir-negative", action="store_true")
    p.add_argument("--steps-per-mm", type=float, help="machine calibration: turn step counts into mm and mm/min")
    p.add_argument("--qa", help="quadrature encoder A channel, e.g. D4"); p.add_argument("--qb", default="D5"); p.add_argument("--cpr", type=float, help="x4 counts per revolution")
    p.add_argument("--pd", help="sigrok decoder, e.g. uart:rx=D0:baudrate=115200 | i2c:scl=D0:sda=D1 | spi:clk=D0:mosi=D1:cs=D2")
    p.add_argument("--ann", help="annotation filter, e.g. uart=rx-data"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out")
    p = sub.add_parser("probe", help="probe factor (x1/x10, sets the scope) and transducers (current clamp: --clamp 10mV/A)")
    p.add_argument("--ch", type=int, required=True); p.add_argument("--x1", action="store_true"); p.add_argument("--x10", action="store_true")
    p.add_argument("--clamp", help="e.g. 10mV/A or 100mV/A (Hantek CC-65 positions)"); p.add_argument("--clear-clamp", action="store_true")
    p = sub.add_parser("mark", help="session marker, e.g. mark 'spindle on' (use later: --from=mark:spindle)"); p.add_argument("text")
    p = sub.add_parser("marks", help="list session markers"); p.add_argument("--from", dest="t0", default="-7d")
    p = sub.add_parser("selftest", help="end-to-end check against the Pi reference (probe on Pi pin 12 + GND)"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("refsig", help="the Pi's reference signal on GPIO18"); p.add_argument("mode", choices=["pwm", "high", "low", "show"]); p.add_argument("--hz", type=float, default=1000.0); p.add_argument("--duty", type=float, default=0.5)
    sub.add_parser("health", help="one-shot check of the whole chain (link, fps, recorder, index, disk, load, firmware)")
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
    p = sub.add_parser("record", help="raw recorder on the Pi (every frame, ring-limited)"); p.add_argument("action", choices=["start", "stop", "status"]); p.add_argument("--max-gb", type=float)
    p = sub.add_parser("records", help="list/fetch stored raw segments"); p.add_argument("action", choices=["list", "fetch"]); p.add_argument("--from", dest="t0"); p.add_argument("--to", dest="t1"); p.add_argument("--every", type=int, default=1); p.add_argument("--limit", type=int, default=5000); p.add_argument("out", nargs="?")
    p = sub.add_parser("find", help="events in the recorded metrics index, e.g. --where 'ch2_mean<4.5,ch1_glitches>0'"); p.add_argument("--from", dest="t0", default="-1h"); p.add_argument("--to", dest="t1", default="now"); p.add_argument("--where", required=True); p.add_argument("--gap", type=float, default=2.0); p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="live: report the moment a condition trips, e.g. --where 'ch2_mean<4.5'"); p.add_argument("--where", required=True); p.add_argument("--for", dest="duration", default="10m"); p.add_argument("--all", action="store_true", help="print every trip, not just the first")
    p = sub.add_parser("decode", help="decoders on the latest frames: steps (step/dir) or uart"); p.add_argument("proto", choices=["steps", "uart"]); p.add_argument("--ch", type=int, default=1, help="step or data channel"); p.add_argument("--dir-ch", type=int, default=2); p.add_argument("--baud", type=int, default=9600); p.add_argument("--bits", type=int, default=8); p.add_argument("--idle-low", action="store_true"); p.add_argument("--frames", type=int, default=3)
    p = sub.add_parser("avg", help="aligned average of N frames (small signals, ripple): levels + plot"); p.add_argument("--ch", type=int, default=1); p.add_argument("--frames", type=int, default=16); p.add_argument("--png")
    p = sub.add_parser("ref", help="reference waveforms on the Pi: save | list | compare"); p.add_argument("action", choices=["save", "list", "compare"]); p.add_argument("name", nargs="?"); p.add_argument("--ch", type=int, default=1); p.add_argument("--note", default=""); p.add_argument("--png")
    p = sub.add_parser("touches", help="probe-touch captures on the Pi (auto range + 4 timebases each): list, or one in full"); p.add_argument("id", nargs="?"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("battery", help="scope battery: level, charging/discharging, time left, warnings, history"); p.add_argument("--hours", type=float, default=6)
    p = sub.add_parser("touch-mode", help="probe-touch capture on/off/resume"); p.add_argument("action", choices=["on", "off", "resume", "show"])
    p = sub.add_parser("cnc", help="CNC bring-up checks with PASS/FAIL: step | encoder | levels | rail | bounce")
    p.add_argument("check", choices=["step", "encoder", "levels", "rail", "bounce"])
    p.add_argument("--ch", type=int, default=1); p.add_argument("--frames", type=int, default=30); p.add_argument("--no-autoset", action="store_true")
    p.add_argument("--json", action="store_true"); p.add_argument("--png")
    p.add_argument("--active", choices=["rising", "falling"], default="rising", help="step: the edge the drive counts (rising = active-high pulses)")
    p.add_argument("--dir-ch", type=int, default=2, help="step: 2 = DIR on CH2, 0 = no DIR probe")
    p.add_argument("--min-pulse"); p.add_argument("--min-space"); p.add_argument("--min-setup"); p.add_argument("--min-hold"); p.add_argument("--max-rate")
    p.add_argument("--no-default-spec", action="store_true", help="step: judge only the limits given (default: a typical driver, 2.5us pulse/space, 5us setup/hold)")
    p.add_argument("--provisional", action="store_true", help="step: allow the provisional 12.5/25 MS/s timebases")
    p.add_argument("--steps-per-mm", type=float)
    p.add_argument("--lines", type=float, help="encoder: pulses per revolution per channel")
    p.add_argument("--family", choices=sorted(CC.FAMILIES), default="3v3"); p.add_argument("--vil"); p.add_argument("--vih")
    p.add_argument("--nominal", help="rail: e.g. 24, 5, 3.3"); p.add_argument("--tol", type=float, default=5.0, help="rail: %% tolerance")
    p.add_argument("--ripple", help="rail: max ripple p-p in volts (default 2%% of nominal)"); p.add_argument("--seconds", type=float, default=5.0); p.add_argument("--tb", default="0x0E", help="rail: timebase code (0x0E = 50 kS/s, 20 ms records)")
    p.add_argument("--edge", choices=["rising", "falling"]); p.add_argument("--vhigh", help="bounce: closed-contact voltage when the line idles low")
    p.add_argument("--window", default="20ms", help="bounce: record length to aim for"); p.add_argument("--timeout", type=float, default=60); p.add_argument("--debounce", help="bounce: the input filter time, e.g. 5ms")
    p = sub.add_parser("replay", help="re-evaluate a stored time window with chosen criteria"); p.add_argument("--from", dest="t0", required=True); p.add_argument("--to", dest="t1", default="now"); p.add_argument("--ch", default="both"); p.add_argument("--every", type=int, default=1); p.add_argument("--limit", type=int, default=5000); p.add_argument("--param", action="append", help="e.g. hyst=0.1 glitch_ratio=0.3 low_res=16"); p.add_argument("--png", help="strip chart of amplitude/mean/freq over time"); p.add_argument("--csv", help="per-frame metrics"); p.add_argument("--json", action="store_true"); p.add_argument("--server", action="store_true", help="run the analysis on the Pi (/api/analyze) instead of locally")
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

    if a.cmd == "la":
        if a.action == "status":
            print(json.dumps(api("/api/la/status", timeout=30))); return
        if a.action == "capture":
            m = api("/api/la/capture", {"rate": a.rate, "seconds": a.seconds, "demo": a.demo, "label": a.label, "trigger": a.trigger},
                    timeout=a.seconds * 3 + 60)
            if "error" in m:
                raise SystemExit("capture failed: " + m["error"])
            print("captured %s: %d samples at %s (%s)%s" % (m["name"], m["samples"], fmt_rate(m["rate"]), m["driver"], ("  " + m["label"]) if m["label"] else ""))
            return
        caps = api("/api/la/captures")
        if a.action == "list":
            for m in caps:
                print("%-22s %s  %9d samples  %-10s %-9s %s" % (m["name"], time.strftime("%m-%d %H:%M:%S", time.localtime(m["t"])), m["samples"], fmt_rate(m["rate"]), m["driver"], m.get("label", "")))
            return
        name = a.name or (caps[-1]["name"] if caps else None)
        if not name:
            raise SystemExit("no captures yet")
        if a.action == "stats":
            qs = []
            if a.step:
                qs.append("step=%s&dir=%s&dir_pos=%s" % (a.step, a.dir, "low" if a.dir_negative else "high"))
                if a.steps_per_mm:
                    qs.append("steps_per_mm=%g" % a.steps_per_mm)
            if a.qa:
                qs.append("qa=%s&qb=%s" % (a.qa, a.qb) + ("&cpr=%g" % a.cpr if a.cpr else ""))
            q = ("?" + "&".join(qs)) if qs else ""
            d = api("/api/la/%s/stats%s" % (name, q), timeout=120)
            m = d["meta"]
            print("%s: %s, %s at %s" % (name, fmt_t(m["samples"] / m["rate"]), m["driver"], fmt_rate(m["rate"])))
            for ch, st in d["channels"].items():
                if "state" in st:
                    print("  %s  constant %s" % (ch, st["state"])); continue
                print("  %s  %6d edges  %s  duty %s  high %s (min %s)  low %s (min %s)%s" % (
                    ch, st["rising"] + st["falling"], fmt_f(st.get("freq_hz")), ("%.1f%%" % (st["duty"] * 100)) if "duty" in st else "--",
                    fmt_t(st.get("high_median_s")), fmt_t(st.get("high_min_s")), fmt_t(st.get("low_median_s")), fmt_t(st.get("low_min_s")),
                    ("  GLITCHES %d/%d" % (st.get("high_glitches", 0), st.get("low_glitches", 0))) if (st.get("high_glitches") or st.get("low_glitches")) else ""))
            if d.get("step_dir"):
                sd = d["step_dir"]
                print("  step/dir (%s/%s): %d steps (+%d / -%d, net %+d), median %s, max %s, DIR changes %d, min DIR setup %s" % (
                    a.step, a.dir, sd["steps"], sd.get("plus", 0), sd.get("minus", 0), sd["net"], fmt_f(sd.get("median_step_rate_hz")),
                    fmt_f(sd.get("max_step_rate_hz")), sd.get("dir_changes", 0), fmt_t(sd.get("min_dir_setup_s"))))
                if "net_mm" in sd:
                    print("  motion: net %+.3f mm, travel %.3f mm, feed median %.0f / max %.0f mm/min" % (
                        sd["net_mm"], sd["travel_mm"], sd.get("median_feed_mm_min", 0), sd.get("max_feed_mm_min", 0)))
            if d.get("quadrature"):
                qd = d["quadrature"]
                print("  quadrature (%s/%s): count %+d (%d edges), %s, ILLEGAL transitions %d, edge rate median %s / max %s%s" % (
                    a.qa, a.qb, qd["count"], qd["edges"], qd["direction"], qd["illegal"], fmt_f(qd.get("median_edge_rate_hz")), fmt_f(qd.get("max_edge_rate_hz")),
                    (", %.3f rev, %.1f RPM" % (qd["revolutions"], qd["rpm"])) if "rpm" in qd else ""))
            return
        if a.action == "decode":
            if not a.pd:
                raise SystemExit("decode needs --pd, e.g. --pd uart:rx=D0:baudrate=115200")
            d = api("/api/la/decode", {"name": name, "pd": a.pd, "ann": a.ann, "limit": a.limit}, timeout=600)
            if d.get("error"):
                print("decoder message: " + d["error"])
            print("%d annotation lines (showing %d)" % (d.get("total_lines", 0), len(d.get("lines", []))))
            for ln in d.get("lines", []):
                print("  " + ln)
            return
        if a.action == "fetch":
            req = urllib.request.Request(URL + "/api/la/%s.bin" % name)
            data = urllib.request.urlopen(req, timeout=300).read()
            out = a.out or name + ".bin"
            open(out, "wb").write(data); print("saved", out, len(data), "bytes"); return

    if a.cmd == "probe":
        if a.x1 or a.x10:
            print(cmd("fpga scope probe %d %d" % (a.ch, 10 if a.x10 else 1)).strip())
        if a.clamp:
            m = re.match(r"^\s*([\d.]+)\s*(m?)V\s*/\s*([A-Za-z]+)\s*$", a.clamp)
            if not m:
                raise SystemExit("--clamp like 10mV/A or 0.1V/A")
            vpu = float(m.group(1)) * (1e-3 if m.group(2) else 1.0)
            api("/api/calib", {"ch": a.ch, "transducer": {"unit": m.group(3), "v_per_unit": vpu}})
            print("CH%d now reads in %s (%g V per %s)" % (a.ch, m.group(3), vpu, m.group(3)))
        if a.clear_clamp:
            api("/api/calib", {"ch": a.ch, "transducer": None}); print("CH%d back to volts" % a.ch)
        f = decode(api("/api/frame.json"))
        print("CH%d: scope probe setting x%d, unit %s" % (a.ch, 10 if f["hdr"]["prb%d" % a.ch] else 1, f.get("unit%d" % a.ch, "V")))
        return

    if a.cmd == "mark":
        m = api("/api/mark", {"text": a.text})
        print("marked %s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["t"])), m["text"])); return

    if a.cmd == "marks":
        for m in api("/api/marks?from=%f" % parse_time(a.t0)):
            print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["t"])), m["text"]))
        return

    if a.cmd == "touches":
        if a.id:
            d = api("/api/touches/" + a.id)
            if a.json:
                print(json.dumps(d, indent=1)); return
            print("touch %s  CH%s  ranges %s" % (d["id"], "+".join(map(str, d["touched"])), d["ranges"]))
            for ch, tbs in d.get("timebases", {}).items():
                for code, v in tbs.items():
                    print("  %s tb %s: %s" % (ch, code, v.get("summary")))
            return
        for t in api("/api/touches"):
            print("%s  %s  CH%s" % (t["id"], time.strftime("%H:%M:%S", time.localtime(t["t0"])), "+".join(map(str, t.get("touched") or []))))
            for ch, line in (t.get("summary") or {}).items():
                print("    " + line)
        return

    if a.cmd == "battery":
        b = api("/api/battery?hours=%g" % a.hours)
        if b.get("mv") is None:
            print("no reading:", "; ".join(b.get("warnings", []))); return
        print("battery %.3f V (~%d%%), %s%s%s" % (b["mv"] / 1000, b["pct_approx"], b["state"],
              (", %+d mV/h" % b["slope_mv_per_h"]) if b.get("slope_mv_per_h") is not None else "",
              (", ~%d min to empty" % b["minutes_to_empty"]) if b.get("minutes_to_empty") is not None else ""))
        for w in b.get("warnings", []):
            print("  !! " + w)
        hist = b.get("history", [])
        if hist:
            print("  history: " + "  ".join("%s %.2f" % (time.strftime("%H:%M", time.localtime(t)), v / 1000) for t, v in hist[-12:]))
        return

    if a.cmd == "touch-mode":
        if a.action == "show":
            print(json.dumps(api("/api/touch"), indent=1)); return
        body = {"resume": True} if a.action == "resume" else {"enabled": a.action == "on"}
        print(json.dumps(api("/api/touch", body))); return

    if a.cmd == "cnc":
        r = cnc(a, info)
        sys.exit(0 if r and (r.get("verdict") or r.get("worst", {}).get("verdict")) in (CC.PASS, CC.INFO) else 1)

    if a.cmd == "selftest":
        res = selftest(info, verbose=not a.json)
        if a.json:
            print(json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in res], indent=1))
        sys.exit(0 if all(ok for _, ok, _ in res) else 1)

    if a.cmd == "refsig":
        print(json.dumps(api("/api/refsig") if a.mode == "show" else refsig(a.mode, a.hz, a.duty)))
        return

    if a.cmd == "health":
        h = api("/api/health")
        sc, rc = h["scope"], h["recorder"]
        ok = []
        def line(label, val, good):
            ok.append(good); print("%s %-10s %s" % ("OK " if good else "!! ", label, val))
        line("link", "%s on %s" % (sc["status"], sc["port"]), sc["status"] == "ok")
        streaming = h.get("hub_mode", "streaming") == "streaming"
        line("frames", "%.1f fps, last %.1fs ago, %d errors, %s" % (sc["fps"] or 0, sc["last_frame_age_s"] or 99, sc["errors"], h.get("hub_mode", "?")),
             ((sc["fps"] or 0) > 5 and (sc["last_frame_age_s"] or 99) < 5) or not streaming)
        line("firmware", " | ".join(sc["firmware"]) if sc["firmware"] else "(no answer)", bool(sc["firmware"]))
        line("recorder", "%s, %d segments, %.1f MB, %s frames" % ("on" if rc["enabled"] else "OFF", rc["segments"] or 0, rc["total_mb"] or 0, rc["frames_total"]), bool(rc["enabled"]))
        line("index", "backlog %s, dropped %s" % (rc["indexer_backlog"], rc["indexer_dropped"]), (rc["indexer_backlog"] or 0) < 100)
        line("disk", "%.1f of %.1f GB free" % (h["disk_free_gb"], h["disk_total_gb"]), h["disk_free_gb"] > 5)
        pw = h.get("power") or {}
        if pw:
            src = "Pi on its own supply" if pw.get("pi_power") == "external" else ("Pi inside, scope on USB power" if pw.get("external") else "Pi inside, ON BATTERY")
            line("power", "%s; scope battery %s mV%s" % (src, pw.get("battery_mv"), ("; vbus sense error: %s" % pw["vbus_err"]) if pw.get("vbus_err") else ""),
                 (pw.get("battery_mv") or 0) > 3500 and not pw.get("vbus_err"))
        line("pi", "load %.2f %.2f %.2f, up %.1f h, %d viewer(s), logging %s" % (*h["load"], (h["pi_uptime_s"] or 0) / 3600, h["viewers"], h["logging"]), h["load"][0] < 3.5)
        line("calib", "gain %s zero %s" % (h["calib"]["gain"], {c: {r: round(v, 1) for r, v in d.items()} for c, d in h["calib"]["zero"].items()}), True)
        print("ALL OK" if all(ok) else "ATTENTION NEEDED")
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
                t = min(tbs, key=lambda t: abs(math.log(t["rate"] / want)) + (0.0 if t["tier"].startswith("measured") else math.log(3.0)))
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
                    t = min(tbs, key=lambda t: abs(math.log(t["rate"] / want)) + (0.0 if t["tier"].startswith("measured") else math.log(3.0)))
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

    if a.cmd == "record":
        if a.action == "status":
            print(json.dumps(api("/api/record"), indent=1)); return
        body = {"action": a.action}
        if a.max_gb: body["max_gb"] = a.max_gb
        print(json.dumps(api("/api/record", body), indent=1)); return

    if a.cmd == "records":
        if a.action == "list":
            segs = api("/api/records")
            tot = 0
            for sg in segs:
                tot += sg["bytes"]
                print("%-28s %s .. %s  %6d frames  %6.1f MB%s" % (sg["name"], time.strftime("%m-%d %H:%M:%S", time.localtime(sg["t0"])),
                      time.strftime("%H:%M:%S", time.localtime(sg["t1"])) if sg["t1"] else "...", sg["frames"], sg["bytes"] / 1e6, "  (active)" if sg.get("active") else ""))
            print("%d segments, %.1f MB" % (len(segs), tot / 1e6)); return
        t0, t1 = parse_time(a.t0 or "-10m"), parse_time(a.t1)
        data = api("/api/records/query?from=%f&to=%f&every=%d&limit=%d" % (t0, t1, a.every, a.limit), timeout=300)
        out = a.out or time.strftime("scope_records_%Y%m%d_%H%M%S.json")
        open(out, "w").write(json.dumps(data)); print("saved %s: %d frames" % (out, len(data["frames"]))); return

    if a.cmd == "decode":
        fs = frames(a.frames)
        f = fs[-1]
        if a.proto == "steps":
            res = [SA.decode_steps(fr["b%d" % a.ch], fr["b%d" % a.dir_ch], fr["rate"]) for fr in fs]
            r = res[-1]
            print("step/dir (step CH%d, dir CH%d), last of %d records: %s" % (a.ch, a.dir_ch, len(fs), json.dumps(r)))
            if "step_rate_hz" in r:
                print("  %d steps in %s (%d +, %d -, net %+d), step rate %s, interval %s..%s, jitter %.1f%%, DIR %s%s" % (
                    r["steps"], fmt_t(r["record_s"]), r["plus"], r["minus"], r["net"], fmt_f(r["step_rate_hz"]), fmt_t(r["interval_min_s"]), fmt_t(r["interval_max_s"]),
                    r["interval_jitter"] * 100, r["dir_level"], " (static)" if r["dir_static"] else " (changes within the record)"))
        else:
            r = SA.decode_uart(f["b%d" % a.ch], f["rate"], a.baud, bits=a.bits, idle_high=not a.idle_low)
            if "error" in r:
                print("uart: " + r["error"])
            else:
                print("uart CH%d %d baud 8N1, %.1f samples/bit: %d byte(s), %d framing error(s)" % (a.ch, a.baud, r["samples_per_bit"], r["count"], r["framing_errors"]))
                print("  hex:  " + r["hex"]); print("  text: " + r["text"])
        return

    if a.cmd == "avg":
        fs = frames(a.frames)
        raws = [f["b%d" % a.ch] for f in fs]
        avg, used = SA.aligned_average(raws)
        if not avg:
            raise SystemExit("could not align %d frames (no rising edges: is the signal periodic?)" % len(raws))
        f = fs[-1]; vpc = f["vpc%d" % a.ch]; z = f.get("zero%d" % a.ch, 0.0)
        ai = bytes(max(0, min(255, int(round(x)))) for x in avg)
        lv = SA.levels(ai, vpc, z)
        raw_lv = SA.levels(f["b%d" % a.ch], vpc, z)
        u = lv["unit"]
        print("CH%d aligned average of %d/%d frames (%d samples): top %s base %s amp %s mean %s AC-rms %s  (single frame AC-rms %s)" % (
            a.ch, used, len(raws), len(avg), fmt(lv["vtop"], u), fmt(lv["vbase"], u), fmt(lv["vamp"], u), fmt(lv["vavg"], u), fmt(lv["vrms_ac"], u), fmt(raw_lv["vrms_ac"], u)))
        if a.png:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            rate = f["rate"]; xs = [(i / rate) if rate else i for i in range(len(avg))]
            vals = [((x - 128 - z) * vpc) if vpc else x for x in avg]
            plt.figure(figsize=(11, 3.5)); plt.plot(xs, vals, color="#1fb4d8" if a.ch == 1 else "#2458e8", lw=1)
            plt.grid(True, alpha=.3); plt.ylabel("CH%d (%s)" % (a.ch, "V" if vpc else "cnt")); plt.xlabel("time (s)" if rate else "sample")
            plt.title("aligned average of %d frames" % used, fontsize=10); plt.tight_layout(); plt.savefig(a.png, dpi=110); print("plot:", a.png)
        return

    if a.cmd == "ref":
        if a.action == "list":
            for r in api("/api/refs"):
                print("%-24s CH%d %s range %d %s  %s" % (r["name"], r["ch"], time.strftime("%Y-%m-%d %H:%M", time.localtime(r["t"])), r["rng"], fmt_rate(r["rate"]) if r["rate"] else "", r["note"]))
            return
        if not a.name:
            raise SystemExit("ref %s needs a name" % a.action)
        if a.action == "save":
            r = api("/api/refs", {"name": a.name, "ch": a.ch, "note": a.note})
            print("saved reference %s: CH%d range %d %s" % (r["name"], r["ch"], r["rng"], fmt_rate(r["rate"]) if r["rate"] else "")); return
        ref = api("/api/refs/%s" % a.name)
        f = frames(1)[-1]
        ch = ref["ch"] if a.ch == 1 and ref["ch"] != 1 else a.ch
        raw = f["b%d" % ch]; vpc = f["vpc%d" % ch]; z = f.get("zero%d" % ch, 0.0)
        ref_raw = base64.b64decode(ref["raw"])
        if f["hdr"]["tb"] != ref["tb"]:
            print("note: timebase now 0x%02X, reference 0x%02X -- comparing anyway (time axis differs)" % (f["hdr"]["tb"], ref["tb"]))
        r = SA.compare_to_reference(raw, ref_raw, f["rate"], vpc, z, ref["vpc"], ref.get("zero", 0.0))
        if "error" in r:
            raise SystemExit("compare: " + r["error"])
        u = r["unit"]
        print("CH%d vs reference '%s' (%s): max |diff| %s, rms diff %s over %d samples%s" % (ch, ref["name"], time.strftime("%Y-%m-%d %H:%M", time.localtime(ref["t"])),
              fmt(r["max_abs_diff"], u), fmt(r["rms_diff"], u), r["samples"], "" if r["aligned_on_edge"] else " (not edge-aligned)"))
        print("  amplitude %s vs %s (%+s), mean %s vs %s, freq %s vs %s (%s)" % (fmt(r["amp_now"], u), fmt(r["amp_ref"], u), fmt(r["amp_change"], u), fmt(r["mean_now"], u), fmt(r["mean_ref"], u),
              fmt_f(r["freq_now"]), fmt_f(r["freq_ref"]), ("%+.2f%%" % r["freq_change_pct"]) if r["freq_change_pct"] is not None else "--"))
        if a.png:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            k = vpc or 1.0; za = (128 + z) if vpc else 0; kr = (ref["vpc"] or 1.0) if ref["vpc"] else 1.0; zr = (128 + ref.get("zero", 0.0)) if ref["vpc"] else 0
            plt.figure(figsize=(11, 3.5)); plt.plot([(x - zr) * kr for x in ref_raw], color="#888", lw=1, label="reference"); plt.plot([(x - za) * k for x in raw], color="#2458e8", lw=1, label="now")
            plt.legend(); plt.grid(True, alpha=.3); plt.xlabel("sample"); plt.tight_layout(); plt.savefig(a.png, dpi=110); print("plot:", a.png)
        return

    if a.cmd == "find":
        t0, t1 = parse_time(a.t0), parse_time(a.t1)
        import urllib.parse
        res = api("/api/events?from=%f&to=%f&gap=%f&where=%s" % (t0, t1, a.gap, urllib.parse.quote(a.where)), timeout=600)
        if a.json:
            print(json.dumps(res, indent=1)); return
        print("%s .. %s  where %s: %d frames scanned, %d matched, %d event(s)%s" % (
            time.strftime("%m-%d %H:%M:%S", time.localtime(t0)), time.strftime("%H:%M:%S", time.localtime(t1)), a.where,
            res["scanned"], res["matched"], len(res["events"]), ("  (indexer dropped %d frames)" % res["indexer_dropped"]) if res.get("indexer_dropped") else ""))
        ch = a.where.split("_")[0]
        for ev in res["events"][:50]:
            sm = ev["sample"].get(ch) or {}
            print("  %s .. %s  %5d frames  extreme: amp %s mean %s freq %s glitches %s clipped %s" % (
                time.strftime("%m-%d %H:%M:%S", time.localtime(ev["first"])), time.strftime("%H:%M:%S", time.localtime(ev["last"])), ev["count"],
                sm.get("amp"), sm.get("mean"), sm.get("freq"), sm.get("glitches"), sm.get("clipped")))
        if len(res["events"]) > 50:
            print("  ... %d more" % (len(res["events"]) - 50))
        if res["events"]:
            print("replay one: scopectl replay --from=%.0f --to=%.0f --ch %s --png /tmp/ev.png" % (res["events"][0]["first"] - 1, res["events"][0]["last"] + 1, ch[-1]))
        return

    if a.cmd == "watch":
        conds = SA_parse_where(a.where)
        t_end = time.time() + (time.time() - parse_time("-" + a.duration))     # "-10m" ago -> +10 min from now
        print("watching for %s until %s ..." % (a.where, time.strftime("%H:%M:%S", time.localtime(t_end))))
        last_seq, tripped = None, 0
        while time.time() < t_end:
            f = api("/api/frame.json")
            if not f or f.get("seq") == last_seq:
                time.sleep(0.05); continue
            last_seq = f["seq"]
            row = {"t": f["t"]}
            for ch in (1, 2):
                m = f["meas"]["ch%d" % ch]
                row["ch%d" % ch] = {"amp": m.get("vamp"), "mean": m.get("vavg"), "rms": m.get("vrms_ac"), "freq": m.get("freq"),
                                    "clipped": int(bool(m.get("clipped"))), "glitches": None, "duty": m.get("duty"), "span": None}
            if all(((row.get(c) or {}).get(fld) is not None) for c, fld, _, _ in conds) and SA_row_matches(row, conds):
                tripped += 1
                vals = ", ".join("%s_%s=%s" % (c, fld, (row.get(c) or {}).get(fld)) for c, fld, _, _ in conds)
                print("TRIPPED at %s: %s" % (time.strftime("%H:%M:%S", time.localtime(f["t"])), vals))
                if not a.all:
                    return
        print("not tripped" if not tripped else "%d trips" % tripped)
        return

    if a.cmd == "replay":
        t0, t1 = parse_time(a.t0), parse_time(a.t1)
        chans = [1, 2] if a.ch == "both" else [int(a.ch)]
        p = parse_params(a.param)
        if a.server:
            q = "&".join("%s=%s" % (k, v) for k, v in (p.as_dict().items()) if k != "mains_band")
            res = {ch: api("/api/analyze?from=%f&to=%f&ch=%d&every=%d&limit=%d&%s" % (t0, t1, ch, a.every, a.limit, q), timeout=600) for ch in chans}
            if a.json: print(json.dumps(res, indent=1, default=str)); return
            for ch in chans:
                r = res[ch]; print("CH%d (%d frames, server): %s" % (ch, r["frames"], r["overall"]["summary"]))
            return
        data = api("/api/records/query?from=%f&to=%f&every=%d&limit=%d" % (t0, t1, a.every, a.limit), timeout=600)
        frs = data["frames"]
        if not frs:
            raise SystemExit("no stored frames between %s and %s" % (time.ctime(t0), time.ctime(t1)))
        for f in frs:
            decode(f)
        print("%d frames from %s to %s" % (len(frs), time.strftime("%H:%M:%S", time.localtime(frs[0]["t"])), time.strftime("%H:%M:%S", time.localtime(frs[-1]["t"]))))
        rows = []
        res = {}
        for ch in chans:
            recs = [rec_from_frame(f, ch, info) for f in frs]
            overall = SA.analyze_records(recs, p)
            res[ch] = {"overall": overall, "per_frame": []}
            for r in recs:
                lv = SA.levels(r["raw"], r["vpc"], r["zero"]); fq = SA.frequency(r["raw"], r["rate"], p); pm = SA.pulse_metrics(r["raw"], r["rate"], p)   # r["raw"] is already the continuous part
                row = {"t": r["t"], "ch": ch, "amp": lv["vamp"], "mean": lv["vavg"], "rms_ac": lv["vrms_ac"], "freq": fq, "clipped": lv["clipped"],
                       "glitches": pm["glitches"] if pm else 0, "duty": pm["duty"] if pm else None, "high_s": pm["high_s"] if pm else None}
                res[ch]["per_frame"].append(row); rows.append(row)
            print("CH%d overall: %s" % (ch, overall["summary"]))
            pf = res[ch]["per_frame"]
            amps = [x["amp"] for x in pf]; means = [x["mean"] for x in pf]; fqs = [x["freq"] for x in pf if x["freq"]]
            u = overall["unit"]
            print("   per frame: amp %s..%s  mean %s..%s  freq %s..%s  frames with glitches %d/%d  clipped %d" % (
                fmt(min(amps), u), fmt(max(amps), u), fmt(min(means), u), fmt(max(means), u), fmt_f(min(fqs)) if fqs else "--", fmt_f(max(fqs)) if fqs else "--",
                sum(1 for x in pf if x["glitches"]), len(pf), sum(1 for x in pf if x["clipped"])))
        if a.csv:
            import csv
            with open(a.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
            print("csv:", a.csv)
        if a.json:
            print(json.dumps(res, indent=1, default=str))
        if a.png:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
            for ch in chans:
                pf = res[ch]["per_frame"]; ts = [x["t"] - frs[0]["t"] for x in pf]; col = "#1fb4d8" if ch == 1 else "#2458e8"
                axes[0].plot(ts, [x["amp"] for x in pf], ".", ms=3, color=col, label="CH%d" % ch)
                axes[1].plot(ts, [x["mean"] for x in pf], ".", ms=3, color=col)
                axes[2].plot(ts, [x["freq"] if x["freq"] else float("nan") for x in pf], ".", ms=3, color=col)
            axes[0].set_ylabel("amplitude (top-base)"); axes[1].set_ylabel("mean"); axes[2].set_ylabel("freq (Hz)"); axes[2].set_xlabel("seconds from %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frs[0]["t"])))
            for ax in axes: ax.grid(True, alpha=.3)
            axes[0].legend(); fig.tight_layout(); fig.savefig(a.png, dpi=110); print("strip chart:", a.png)
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
