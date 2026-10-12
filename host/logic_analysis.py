"""logic_analysis — 8-channel logic analyzer support (sigrok fx2lafw, the $12 24 MHz boxes).

Runs on the Pi next to scope_server.py (imported by it) and on the Mac (stats on fetched captures).
Captures are on demand (the Pi Zero cannot stream 24 MS/s continuously): sigrok-cli writes raw
binary (one byte per sample, bit n = channel Dn) plus a JSON sidecar into <records>/la/. Stats
(edges, frequency, duty, pulse widths, glitches, step/dir counting) are numpy; protocol decoding
is sigrok's own decoders (uart, i2c, spi, can, pwm, stepper_motor, ...) run on the stored file, so
any capture can be re-decoded later with different settings.

Driver: "fx2lafw" when the analyzer is plugged in; "demo" = sigrok's built-in fake device, used to
test the whole pipeline without hardware.
"""
import json, os, re, subprocess, threading, time

try:
    import numpy as np
except Exception:
    np = None

MAX_SAMPLES = 64_000_000          # 64 MB per capture (Pi Zero 2 W has ~400 MB RAM)


def _sr(args, timeout):
    return subprocess.run(["sigrok-cli"] + args, capture_output=True, text=True, timeout=timeout)


def parse_rate(x):
    """'4M', '500k', '24MHz', 1e6 -> float Hz."""
    if isinstance(x, (int, float)):
        return float(x)
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgG]?)", str(x))
    if not m:
        raise ValueError("bad rate %r" % x)
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


def rate_arg(hz):
    return ("%gM" % (hz / 1e6)) if hz >= 1e6 else ("%gk" % (hz / 1e3)) if hz >= 1e3 else ("%g" % hz)


class LogicAnalyzer:
    def __init__(self, root):
        self.root = os.path.join(root, "la")
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.Lock()
        self.busy = False

    # ---- device ----
    def scan(self):
        try:
            r = _sr(["--scan"], 20)
        except Exception as e:
            return {"present": False, "error": str(e)}
        found = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("fx2lafw")]
        return {"present": bool(found), "devices": found}

    # ---- capture ----
    def capture(self, rate="1M", seconds=1.0, demo=False, label="", trigger=None):
        hz = parse_rate(rate)
        n = int(hz * float(seconds))
        if n <= 0 or n > MAX_SAMPLES:
            raise ValueError("rate x seconds = %d samples (limit %d)" % (n, MAX_SAMPLES))
        drv = "demo" if demo else "fx2lafw"
        name = time.strftime("la_%Y%m%d_%H%M%S")
        path = os.path.join(self.root, name + ".bin")
        args = ["-d", drv, "--config", "samplerate=%s" % rate_arg(hz), "--samples", str(n),
                "--channels", ",".join("D%d" % i for i in range(8)), "-O", "binary", "-o", path]
        if trigger:                      # sigrok syntax, e.g. "D0=r" (rising) / "D2=0"
            args += ["--triggers", trigger, "--wait-trigger"]
        with self.lock:
            if self.busy:
                raise RuntimeError("a capture is already running")
            self.busy = True
        t0 = time.time()
        try:
            r = _sr(args, timeout=float(seconds) * 3 + 30)
        finally:
            self.busy = False
        if r.returncode != 0 or not os.path.exists(path):
            raise RuntimeError("sigrok-cli failed: %s" % (r.stderr.strip() or r.stdout.strip())[:400])
        meta = {"name": name, "t": t0, "rate": hz, "samples": os.path.getsize(path), "channels": 8,
                "driver": drv, "label": label, "trigger": trigger}
        with open(os.path.join(self.root, name + ".json"), "w") as f:
            json.dump(meta, f)
        return meta

    def list(self):
        out = []
        for fn in sorted(os.listdir(self.root)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(self.root, fn)) as f:
                        out.append(json.load(f))
                except Exception:
                    pass
        return out

    def path(self, name):
        name = re.sub(r"[^\w.-]", "", name)
        return os.path.join(self.root, name + ".bin"), os.path.join(self.root, name + ".json")

    def meta(self, name):
        with open(self.path(name)[1]) as f:
            return json.load(f)

    # ---- decode (sigrok protocol decoders on the stored file) ----
    def decode(self, name, pd, ann=None, limit=2000):
        b, _ = self.path(name)
        m = self.meta(name)
        # the binary input names its channels "0".."7"; accept the analyzer's D0..D7 names in pd
        pd_in = re.sub(r"=D([0-7])\b", r"=\1", pd)
        args = ["-i", b, "-I", "binary:numchannels=8:samplerate=%d" % int(m["rate"]), "-P", pd_in]
        if ann:
            args += ["-A", ann]
        r = _sr(args, timeout=300)
        lines = r.stdout.splitlines()
        return {"name": name, "pd": pd, "ann": ann, "lines": lines[:limit], "total_lines": len(lines),
                "error": r.stderr.strip()[:400] if r.returncode else None}


# ------------------------------------------------------------------ stats (numpy)
def load(path):
    return np.fromfile(path, dtype=np.uint8)


def channel_stats(data, rate, glitch_ratio=0.2):
    """Per channel: level, transitions, frequency, duty, min/median pulse widths, glitch count."""
    out = {}
    for ch in range(8):
        bits = ((data >> ch) & 1).astype(np.int8)
        d = np.diff(bits)
        rises = np.flatnonzero(d == 1) + 1
        falls = np.flatnonzero(d == -1) + 1
        st = {"rising": int(rises.size), "falling": int(falls.size), "high_fraction": float(bits.mean())}
        if rises.size == 0 and falls.size == 0:
            st["state"] = "high" if bits[0] else "low"
            out["D%d" % ch] = st
            continue
        if rises.size >= 2:
            per = np.diff(rises)
            st["freq_hz"] = float(rate / np.median(per))
            st["period_jitter"] = float((per.max() - per.min()) / np.median(per))
        # pulse widths: high = rise -> next fall, low = fall -> next rise
        highs = lows = np.array([], dtype=np.int64)
        if rises.size and falls.size:
            fi = np.searchsorted(falls, rises)
            ok = fi < falls.size
            highs = falls[fi[ok]] - rises[ok]
            ri = np.searchsorted(rises, falls)
            ok = ri < rises.size
            lows = rises[ri[ok]] - falls[ok]
        for nm, w in (("high", highs), ("low", lows)):
            if w.size:
                med = float(np.median(w))
                st[nm + "_median_s"] = med / rate
                st[nm + "_min_s"] = float(w.min()) / rate
                st[nm + "_glitches"] = int((w < glitch_ratio * med).sum())
        if highs.size and lows.size:
            st["duty"] = float(np.median(highs) / (np.median(highs) + np.median(lows)))
        out["D%d" % ch] = st
    return out


def step_dir(data, rate, step=0, dirc=1, dir_high_positive=True):
    """Count step pulses (rising edges on `step`), signed by `dirc`'s level at each edge;
    step rate over time (per 10 ms bins) and the largest instantaneous rate."""
    s = ((data >> step) & 1).astype(np.int8)
    dr = ((data >> dirc) & 1).astype(np.int8)
    rises = np.flatnonzero(np.diff(s) == 1) + 1
    if rises.size == 0:
        return {"steps": 0, "net": 0}
    pos = dr[rises] == (1 if dir_high_positive else 0)
    plus, minus = int(pos.sum()), int((~pos).sum())
    out = {"steps": int(rises.size), "plus": plus, "minus": minus, "net": plus - minus,
           "dir_changes": int((np.diff(dr) != 0).sum())}
    if rises.size >= 2:
        iv = np.diff(rises) / rate
        out["max_step_rate_hz"] = float(1.0 / iv.min())
        out["median_step_rate_hz"] = float(1.0 / np.median(iv))
        # direction setup time: from the last DIR change before a step to that step
        chg = np.flatnonzero(np.diff(dr) != 0) + 1
        if chg.size:
            j = np.searchsorted(chg, rises) - 1
            ok = j >= 0
            setup = (rises[ok] - chg[j[ok]]) / rate
            first_after = setup[np.r_[True, np.diff(j[ok]) != 0]] if setup.size else setup
            if first_after.size:
                out["min_dir_setup_s"] = float(first_after.min())
    return out


def steps_to_motion(sd, steps_per_mm):
    """Add distance (mm) and feed (mm/min) to a step_dir() result."""
    if not steps_per_mm or not sd.get("steps"):
        return sd
    out = dict(sd)
    out["net_mm"] = sd["net"] / steps_per_mm
    out["travel_mm"] = sd["steps"] / steps_per_mm
    if sd.get("median_step_rate_hz"):
        out["median_feed_mm_min"] = sd["median_step_rate_hz"] / steps_per_mm * 60.0
        out["max_feed_mm_min"] = sd["max_step_rate_hz"] / steps_per_mm * 60.0
    return out


def quadrature(data, rate, a=0, b=1, counts_per_rev=None):
    """Quadrature (A/B) decoder: x4 counting on every edge of A and B. Returns the net count,
    direction, the number of ILLEGAL transitions (both lines changed at once: noise, missed
    samples or a too-slow sample rate), max/median edge rate and, with counts_per_rev
    (x4 counts per revolution), revolutions and RPM."""
    A = ((data >> a) & 1).astype(np.int8)
    B = ((data >> b) & 1).astype(np.int8)
    st = (A << 1) | B                                      # 0..3 Gray state
    ch = np.flatnonzero(np.diff(st) != 0) + 1
    if ch.size == 0:
        return {"count": 0, "edges": 0, "illegal": 0, "direction": "none"}
    prev, cur = st[ch - 1], st[ch]
    # Gray sequence 00->01->11->10->00 (= 0,1,3,2) is +1
    order = np.array([0, 1, 3, 2])
    pos = np.empty(4, dtype=np.int8); pos[order] = np.arange(4)
    d = (pos[cur] - pos[prev]) % 4                          # 1 = +1, 3 = -1, 2 = illegal
    plus, minus, illegal = int((d == 1).sum()), int((d == 3).sum()), int((d == 2).sum())
    out = {"count": plus - minus, "edges": int(ch.size), "plus": plus, "minus": minus, "illegal": illegal,
           "direction": "A leads B (+)" if plus > minus else "B leads A (-)" if minus > plus else "none"}
    if ch.size >= 2:
        iv = np.diff(ch) / rate
        out["max_edge_rate_hz"] = float(1.0 / iv.min())
        out["median_edge_rate_hz"] = float(1.0 / np.median(iv))
    if counts_per_rev:
        out["revolutions"] = out["count"] / counts_per_rev
        dur = len(A) / rate
        out["rpm"] = out["revolutions"] / dur * 60.0
    return out
