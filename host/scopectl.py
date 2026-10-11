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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_analysis as SA
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
    return {"raw": c["raw"], "rate": f["rate"], "vpc": c["vpc"], "zero": f.get("zero%d" % ch, 0.0) or 0.0,
            "probe": c["probe"], "cpl": c["cpl"], "rng": c["rng"], "tier": info["range_tier"][c["rng"]],
            "enabled": bool(c["en"]), "channel": ch, "counts_per_div": info["counts_per_div"],
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
    """unix seconds | 'now' | '-10m' / '-2h' / '-30s' (ago) | 'YYYY-MM-DD HH:MM[:SS]' local."""
    if t is None or t == "now":
        return time.time()
    t = str(t).strip()
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

    if a.cmd == "health":
        h = api("/api/health")
        sc, rc = h["scope"], h["recorder"]
        ok = []
        def line(label, val, good):
            ok.append(good); print("%s %-10s %s" % ("OK " if good else "!! ", label, val))
        line("link", "%s on %s" % (sc["status"], sc["port"]), sc["status"] == "ok")
        line("frames", "%.1f fps, last %.1fs ago, %d errors" % (sc["fps"] or 0, sc["last_frame_age_s"] or 99, sc["errors"]), (sc["fps"] or 0) > 5 and (sc["last_frame_age_s"] or 99) < 5)
        line("firmware", " | ".join(sc["firmware"]) if sc["firmware"] else "(no answer)", bool(sc["firmware"]))
        line("recorder", "%s, %d segments, %.1f MB, %s frames" % ("on" if rc["enabled"] else "OFF", rc["segments"] or 0, rc["total_mb"] or 0, rc["frames_total"]), bool(rc["enabled"]))
        line("index", "backlog %s, dropped %s" % (rc["indexer_backlog"], rc["indexer_dropped"]), (rc["indexer_backlog"] or 0) < 100)
        line("disk", "%.1f of %.1f GB free" % (h["disk_free_gb"], h["disk_total_gb"]), h["disk_free_gb"] > 5)
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
                lv = SA.levels(r["raw"], r["vpc"], r["zero"]); fq = SA.frequency(r["raw"], r["rate"], p); pm = SA.pulse_metrics(r["raw"], r["rate"], p)
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
