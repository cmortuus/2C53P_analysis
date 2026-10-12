#!/usr/bin/env python3
"""scope_server — runs on the Pi next to the FNIRSI 2C53T (OpenScope firmware + `scope dumpbin`).

Owns the scope's USB CDC shell and serves:
  :8080  live web viewer (web/index.html) + JSON API
           GET  /                 viewer
           GET  /api/info         calibration tables, current settings, link status
           GET  /api/stream       Server-Sent Events: one JSON frame per capture
           GET  /api/frame.json   latest frame          GET /api/frame.csv  latest frame as CSV
           GET  /api/screen.png   the device's own LCD (screen dumpbin), ?scale=1..4&hold=1|0
           GET/POST /api/log       measurement logger (POST {action: start|stop|clear}); GET /api/log.csv
           GET/POST /api/calib     per-unit gain/zero per (ch, range), persisted in calib.json
           GET/POST /api/record    raw recorder status / {action: start|stop|disable, max_gb}
           GET  /api/records       stored segments; /api/records/<name>.bin download;
                /api/records/query?from=&to=&every=&limit=   frames in a window (JSON, base64)
           GET  /api/analyze?from=&to=&ch=&every=&limit=&hyst=..   server-side re-evaluation (scope_analysis)
           GET  /api/events?from=&to=&where=ch2_mean<4.5,ch1_glitches>0&gap=2   events from the per-frame metrics index
           GET/POST /api/refs, GET /api/refs/<name>   reference waveforms (scopectl ref save|compare)
           GET/POST /api/refsig    the Pi's calibration signal on GPIO18: {mode: pwm, hz, duty} | {mode: high|low}
           POST /api/mark {text}, GET /api/marks?from&to   session markers ("spindle on") kept with the records
           POST /api/cmd          {"cmd": "<shell command>"}   (allow-listed scope controls only)
           POST /api/run          {"run": true|false}           (pause/resume streaming)
  :7001  raw shell proxy, line based — drop-in for the old socat bridge, so
         `host/t_scope.py --tcp scope-pi.local:7001` keeps working. Commands from
         TCP clients and from the acquisition loop are serialised on one lock.

Only the standard library + pyserial. Run:  python3 scope_server.py [--http 8080] [--tcp 7001]
"""
import argparse, base64, glob, json, math, os, re, socket, socketserver, struct, subprocess, threading, time, zlib
sys_path_dir = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, sys_path_dir)
try:
    import scope_analysis as SA          # shared with host/scopectl.py; deployed next to this file
except Exception as _e:                  # analysis endpoints then report the import error
    SA = None
    SA_ERR = str(_e)
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import serial

HDR = re.compile(rb"SCOPEBIN ((?:\w+=[\w.+-]+ ?)+)\r\n")
END = re.compile(rb"SCOPEBIN END gen=(\d+)\r\n")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# --- calibration tables (OpenScope firmware 2eae008: src/ui/scope_timebase.c, scope_cal.c) ---
# Per-unit corrections measured against the Pi's own crystal (hardware PWM on GPIO18,
# 2026-10-10): every "measured" OpenScope code was within 0.2 %, the provisional 0x0D read
# 1.05 % slow -> it is the round 125 kS/s. Overrides replace the firmware's rate_hz when set.
RATE_OVERRIDE = {0x0D: 124960.0}

# Per-unit vertical calibration, persisted next to the server (calib.json):
#   {"gain": {"2": {"6": 1.062}}, "zero": {"2": {"6": 16.4}}}
#   gain multiplies the firmware's volts-per-count for (channel, range);
#   zero is the ADC count the input reads with the probe on ground, minus 128,
#   subtracted before scaling. Written by POST /api/calib (scopectl zero/gain).
CALIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib.json")
CALIB = {"gain": {}, "zero": {}}


def calib_load():
    global CALIB
    try:
        with open(CALIB_PATH) as f:
            d = json.load(f)
        CALIB = {"gain": d.get("gain", {}), "zero": d.get("zero", {})}
        log("calibration loaded:", CALIB)
    except FileNotFoundError:
        pass
    except Exception as e:
        log("calibration file ignored:", e)


def calib_save():
    tmp = CALIB_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(CALIB, f, indent=1)
    os.replace(tmp, CALIB_PATH)


def calib_gain(ch, rng):
    return float(CALIB["gain"].get(str(ch), {}).get(str(rng), 1.0))


def calib_zero(ch, rng):
    return float(CALIB["zero"].get(str(ch), {}).get(str(rng), 0.0))
# 0x0D..0x14 from OpenScope (bench unit #1); 0x06..0x0C measured here against the Pi's PWM
# (host/t_tbsweep.py, 2026-10-11): the 1-2-5 ladder continues, records are coherent down to
# 0x08. 0x05 and faster are NOT uniform-rate records and are deliberately absent.
TIMEBASE_RATE = {0x06: 25250000.0, 0x07: 12559464.0,
                 0x08: 5001639.0, 0x09: 2500316.0, 0x0A: 1250142.0, 0x0B: 499995.0, 0x0C: 249873.0,
                 0x0D: 123662.7, 0x0E: 49930.1, 0x0F: 24979.1, 0x10: 12490.0,
                 0x11: 4990.8, 0x12: 2494.9, 0x13: 1250.4, 0x14: 500.2}
TIMEBASE_TIER = {0x06: "provisional", 0x07: "provisional"}   # others: "measured" (0x0D re-measured 2026-10-11)
SAMPLES_PER_DIV = 32                                        # firmware renderer convention
COUNTS_PER_DIV = 32
MV_PER_COUNT = {1: [0, 0, 0, 0, 14.08, 21.83, 42.95, 88.42, 279.05, 352.17],
                2: [0, 0, 0, 0, 9.49, 20.96, 41.71, 83.79, 223.71, 425.00]}
RANGE_TIER = ["none"] * 4 + ["provisional", "measured", "measured", "measured", "provisional", "provisional"]

# Commands the web UI may send. Anything else (mem write, flash, fwload…) is refused over HTTP.
ALLOWED = [re.compile(p) for p in (
    r"^fpga scope (timebase|vdiv|level|trigmode|edge|graticule|softtrig|hpos|measure|freq|cal)( [\w.\-]+){0,2}$",
    r"^idle( off| \d{1,3})?$",
    r"^power$",   # not `center`: it stalls the CDC shell for seconds (link drop, 2026-10-10)
    r"^btn (ch1|ch2|move|select|trigger|prm|auto|save|menu|up|down|left|right|ok)( \d+)?$",
    r"^(version|status|settings|help)$",
)]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Scope:
    """Serialised access to the OpenScope debug shell over USB CDC."""

    def __init__(self):
        self.ser = None
        self.lock = threading.RLock()
        self.port = None

    # ---- connection ----
    def _find_port(self):
        for pat in ("/dev/serial/by-id/*Artery*", "/dev/serial/by-id/*", "/dev/ttyACM*"):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[0]
        return None

    def ensure(self):
        if self.ser is not None:
            return True
        port = self._find_port()
        if not port:
            return False
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.005, write_timeout=2)
            self.port = port
            time.sleep(0.2)
            self.ser.write(b"\r")
            self._read_until_prompt(1.0)
            log("connected", port)
            self.open_failures = 0
            return True
        except (serial.SerialException, OSError) as e:
            log("open failed:", e)
            self.ser = None
            # After the scope resets (firmware install, pinhole reset) the Pi's cdc_acm can
            # keep a stale port: opens succeed but every write times out. Re-authorising the
            # USB device re-enumerates it; proven to clear the state (2026-10-10).
            self.open_failures = getattr(self, "open_failures", 0) + 1
            if "timeout" in str(e).lower() and self.open_failures % 3 == 0:
                log("re-authorising USB device 1-1")
                subprocess.call(["sudo", "sh", "-c",
                                 "D=$(readlink -f /sys/bus/usb/devices/1-1); echo 0 > $D/authorized; sleep 2; echo 1 > $D/authorized"],
                                timeout=20)
            return False

    def drop(self, why):
        log("link lost:", why)
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # ---- low level ----
    def _read_until_prompt(self, timeout, buf=b""):
        end = time.time() + timeout
        quiet_since = None
        while time.time() < end:
            d = self.ser.read(8192)
            if d:
                buf += d
                quiet_since = None
                continue
            if buf.endswith(b"> "):
                if quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since > 0.03:
                    break
        return buf

    def raw(self, line, timeout=3.0):
        """Send one shell line, return the raw reply bytes (echo + output + prompt)."""
        with self.lock:
            if not self.ensure():
                raise ConnectionError("scope not connected")
            try:
                self.ser.reset_input_buffer()
                self.ser.write(line.encode() + b"\r")
                if line.startswith("scope dumpbin"):
                    return self._read_dumpbin(timeout)
                return self._read_until_prompt(timeout)
            except (serial.SerialException, OSError) as e:
                self.drop(e)
                raise ConnectionError(str(e))

    def _read_dumpbin(self, timeout):
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            buf += self.ser.read(8192)
            m = HDR.search(buf)
            if m:
                n = int(dict(t.split(b"=", 1) for t in m.group(1).split())[b"len"])
                while len(buf) < m.end() + n and time.time() < end:
                    buf += self.ser.read(m.end() + n - len(buf))
                # The record always ends "SCOPEBIN END gen=N\r\n> " -- return the moment the
                # prompt is in, no quiet-time wait (that cost ~50 ms per frame on the Pi).
                while time.time() < end:
                    e = END.search(buf, m.end() + n)
                    if e and buf.find(b"> ", e.end()) >= 0:
                        return buf
                    buf += self.ser.read(max(1, self.ser.in_waiting))
                return buf
            if b"ERR" in buf and buf.endswith(b"> "):
                return buf
        return buf

    # ---- device screenshot (OpenScope `screen dumpbin`: 320x240, 4-bit palette) ----
    SCREEN_HDR = re.compile(rb"SCREENBIN x=(\d+) y=(\d+) w=(\d+) h=(\d+) format=indexed4 len=(\d+) crc32=([0-9A-F]{8})\r\n")
    SCREEN_PAL = [0x0000, 0xFFFF, 0xF800, 0x07E0, 0x001F, 0xFFE0, 0x07FF, 0xF81F,
                  0x2104, 0x8410, 0xFCA0, 0x055F, 0x2945, 0x18C3, 0x3186, 0x6BB0]

    def screen(self, timeout=20.0):
        """Return (w, h, rgb_rows) of the LCD as the firmware's 16-colour shadow sees it.
        The firmware sends the dump in two passes and its CRC only matches on a held screen,
        so the CRC is reported but not enforced."""
        with self.lock:
            if not self.ensure():
                raise ConnectionError("scope not connected")
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"screen dumpbin\r")
                end = time.time() + timeout
                buf = b""
                m = None
                while time.time() < end:
                    buf += self.ser.read(max(1, self.ser.in_waiting))
                    m = self.SCREEN_HDR.search(buf)
                    if m:
                        break
                    if b"ERR" in buf and buf.endswith(b"> "):
                        raise ValueError(buf.decode(errors="replace").strip())
                if not m:
                    raise ValueError("no SCREENBIN header")
                w, h, n = int(m.group(3)), int(m.group(4)), int(m.group(5))
                while len(buf) < m.end() + n and time.time() < end:
                    buf += self.ser.read(max(1, min(self.ser.in_waiting, m.end() + n - len(buf)) or 1))
                data = buf[m.end():m.end() + n]
                self._read_until_prompt(1.0)
            except (serial.SerialException, OSError) as e:
                self.drop(e)
                raise ConnectionError(str(e))
        pal = [(((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31)
               for c in self.SCREEN_PAL]
        row_len = (w + 1) // 2
        rows = []
        for y in range(h):
            r = bytearray()
            line = data[y * row_len:(y + 1) * row_len]
            for x in range(w):
                b = line[x >> 1] if (x >> 1) < len(line) else 0
                r += bytes(pal[(b & 0x0F) if (x & 1) else (b >> 4)])
            rows.append(bytes(r))
        return w, h, rows

    def cmd(self, line, timeout=3.0):
        """Send a shell command, return its text output without echo and prompt."""
        out = self.raw(line, timeout).decode(errors="replace").replace("\r", "")
        lines = out.split("\n")
        if lines and lines[0].strip() == line.strip():
            lines = lines[1:]
        return "\n".join(l for l in lines if l.strip() not in (">", "")).strip()

    def frame(self, fast=True):
        """One capture record → (header dict, ch1 bytes, ch2 bytes). Raises on CRC/tear."""
        buf = self.raw("scope dumpbin fast" if fast else "scope dumpbin", 3.0)
        m = HDR.search(buf)
        if not m:
            raise ValueError("no SCOPEBIN header: %r" % buf[:80])
        h = {}
        for tok in m.group(1).decode().split():
            k, v = tok.split("=", 1)
            h[k] = int(v, 16) if k == "crc32" else int(v, 0)
        n = h["len"]
        data = buf[m.end():m.end() + n]
        if len(data) < n:
            raise ValueError("short record")
        e = END.search(buf, m.end() + n)
        if zlib.crc32(data) & 0xFFFFFFFF != h["crc32"]:
            raise ValueError("crc mismatch (torn record)")
        if e and int(e.group(1)) != h["gen"]:
            raise ValueError("torn record (gen changed)")
        half = h["n"]
        return h, data[:half], data[half:2 * half]


# --------------------------------------------------------------------------------------------
def png_rgb(w, h, rows, scale=1):
    """Minimal PNG encoder (8-bit RGB, no filter), nearest-neighbour upscale by `scale`."""
    import struct
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    raw = bytearray()
    for r in rows:
        if scale > 1:
            r = b"".join(r[i:i + 3] * scale for i in range(0, len(r), 3))
        for _ in range(scale):
            raw += b"\x00" + r
    ihdr = struct.pack(">IIBBBBB", w * scale, h * scale, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def measure(raw, vpc, rate, zero_counts=0.0):
    """Basic measurements on one channel. Volts when vpc>0, else ADC counts."""
    n = len(raw)
    if n == 0:
        return {}
    mn, mx = min(raw), max(raw)
    mean = sum(raw) / n
    var = sum((x - mean) ** 2 for x in raw) / n
    unit = "V" if vpc else "cnt"
    s = vpc if vpc else 1.0
    zero = (128 + zero_counts) if vpc else 0
    # Top/base = median of the samples above/below the mean: a square wave's flat levels,
    # immune to noise peaks and overshoot (max-min over-reads by 2-3 counts on coarse ranges).
    if SA is not None:
        vtop, vbase = SA.plateau_levels(raw, mean)       # sub-count, edge/overshoot-robust
    else:
        above = sorted(x for x in raw if x > mean) or [mean]
        below = sorted(x for x in raw if x <= mean) or [mean]
        vtop, vbase = above[len(above) // 2], below[len(below) // 2]
    out = {"unit": unit, "vmax": (mx - zero) * s, "vmin": (mn - zero) * s, "vpp": (mx - mn) * s,
           "vtop": (vtop - zero) * s, "vbase": (vbase - zero) * s, "vamp": (vtop - vbase) * s,
           "vavg": (mean - zero) * s, "vrms_ac": math.sqrt(var) * s,
           "clipped": bool(mn <= 0 or mx >= 227), "freq": None, "period": None, "duty": None}
    span = mx - mn
    if span >= 4:
        hi, lo = mean + 0.15 * span, mean - 0.15 * span
        state = raw[0] > mean
        rises = []
        for i, x in enumerate(raw):
            if state and x < lo:
                state = False
            elif not state and x > hi:
                state = True
                # sub-sample edge: interpolate the mean-level crossing just before i
                j = i
                while j > 0 and raw[j - 1] > mean:
                    j -= 1
                if j > 0 and raw[j] != raw[j - 1]:
                    rises.append(j - 1 + (mean - raw[j - 1]) / (raw[j] - raw[j - 1]))
                else:
                    rises.append(float(j))
        if len(rises) >= 3:
            # Median of the individual cycle periods: a free-run record holds one time seam
            # (the FPGA write pointer), which corrupts exactly one cycle; (last-first)/n
            # would spread that error over the whole estimate (up to 1/cycles, 12% at 0x0D).
            pers = sorted(b - a for a, b in zip(rises, rises[1:]))
            med = pers[len(pers) // 2] if len(pers) % 2 else (pers[len(pers) // 2 - 1] + pers[len(pers) // 2]) / 2
            # mean of the periods that agree with the median (drops the seam cycle only);
            # a plain median is biased by integer edge positions on fast-edged signals.
            good = [q for q in pers if abs(q - med) <= max(1.5, 0.15 * med)]
            per = sum(good) / len(good) if good else med
            seg = raw[int(rises[0]):int(rises[-1])]
            out["duty"] = sum(1 for x in seg if x > mean) / max(1, len(seg))
            if rate:
                out["period"] = per / rate
                out["freq"] = rate / per
            else:
                out["period_samples"] = per
    return out


class Recorder:
    """Raw frame recorder: every frame the hub receives is appended to a segment file so any
    window can be re-evaluated later with different criteria (scopectl replay, /api/analyze).
    Record: b"SR1" + <d t><I n_json> + json(hdr, rate, vpc1/2, zero1/2, seq) + ch1[1024] + ch2[1024]
    Segments rotate hourly or at 64 MB; the oldest are deleted beyond max_bytes (ring)."""
    MAGIC = b"SR1"
    SEG_BYTES = 64 * 1024 * 1024
    SEG_SECONDS = 3600

    def __init__(self, root, max_bytes):
        self.root = root
        self.max_bytes = max_bytes
        self.enabled = True
        self.forced = False                   # keep the hub polling even with no viewer (only on external power)
        self.user_forced = False              # `record start`: poll + record even on battery
        self.paused_until = 0.0               # after a power-off notice: write nothing until then
        self.fh = None
        self.cur = None                       # {"name","path","t0","t1","frames","bytes"}
        self.segments = []
        self.lock = threading.Lock()
        self.last_flush = 0.0
        os.makedirs(root, exist_ok=True)
        self._scan()

    # ---- index ----
    def _scan(self):
        for path in sorted(glob.glob(os.path.join(self.root, "rec_*.bin"))):
            idx = path[:-4] + ".idx.json"
            try:
                with open(idx) as f:
                    meta = json.load(f)
            except Exception:
                meta = self._index_file(path)
                try:
                    with open(idx, "w") as f:
                        json.dump(meta, f)
                except Exception:
                    pass
            if meta:
                self.segments.append(meta)

    def _index_file(self, path):
        t0 = t1 = None
        frames = 0
        try:
            for t, _, _, _ in self.iter_file(path):
                frames += 1
                t0 = t if t0 is None else t0
                t1 = t
        except Exception:
            pass
        return {"name": os.path.basename(path), "path": path, "t0": t0, "t1": t1, "frames": frames,
                "bytes": os.path.getsize(path)}

    @classmethod
    def iter_file(cls, path, t_from=None, t_to=None):
        """Yield (t, meta_dict, ch1_bytes, ch2_bytes) from a segment file."""
        with open(path, "rb") as f:
            while True:
                head = f.read(3 + 12)
                if len(head) < 15:
                    return
                if head[:3] != cls.MAGIC:
                    return                                   # torn tail: stop
                t, n = struct.unpack("<dI", head[3:15])
                meta_b = f.read(n)
                if len(meta_b) < n:
                    return
                try:
                    meta = json.loads(meta_b)
                except ValueError:
                    return
                m = meta.get("n", 1024)
                data = f.read(2 * m)
                if len(data) < 2 * m:
                    return
                if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
                    continue
                yield t, meta, data[:m], data[m:]

    # ---- writing ----
    def _rotate(self):
        if self.fh:
            self.fh.close()
            self.fh = None
            if self.cur:
                try:
                    with open(self.cur["path"][:-4] + ".idx.json", "w") as f:
                        json.dump(self.cur, f)
                except Exception:
                    pass
        name = time.strftime("rec_%Y%m%d_%H%M%S.bin")
        path = os.path.join(self.root, name)
        k = 2
        while os.path.exists(path):                     # a quiesce + resume within the same second
            name = time.strftime("rec_%Y%m%d_%H%M%S") + "_%d.bin" % k
            path = os.path.join(self.root, name)
            k += 1
        self.fh = open(path, "ab")
        self.cur = {"name": name, "path": path, "t0": None, "t1": None, "frames": 0, "bytes": 0}
        self.segments.append(self.cur)
        self._trim()

    def _trim(self):
        total = sum(sg["bytes"] for sg in self.segments)
        while total > self.max_bytes and len(self.segments) > 1:
            old = self.segments.pop(0)
            total -= old["bytes"]
            for pth in (old["path"], old["path"][:-4] + ".idx.json"):
                try:
                    os.remove(pth)
                except OSError:
                    pass
            log("recorder: dropped", old["name"])

    def quiesce(self, why=""):
        """Make everything on disk consistent NOW (power may drop in ~3 s): flush + fsync the
        active segment, write its index, close it, flush the metrics index, sync the fs."""
        with self.lock:
            if self.fh:
                try:
                    self.fh.flush(); os.fsync(self.fh.fileno())
                except Exception:
                    pass
                self.fh.close(); self.fh = None
                if self.cur:
                    try:
                        with open(self.cur["path"][:-4] + ".idx.json", "w") as f:
                            json.dump(self.cur, f); f.flush(); os.fsync(f.fileno())
                    except Exception:
                        pass
                self.cur = None
        if indexer is not None:
            indexer.flush()
        try:
            os.sync()
        except Exception:
            pass
        if why:
            log("recorder quiesced:", why)

    def write(self, fr, c1, c2):
        if not self.enabled or time.time() < self.paused_until:
            return
        meta = {"hdr": fr["hdr"], "rate": fr["rate"], "vpc1": fr["vpc1"], "vpc2": fr["vpc2"],
                "zero1": fr.get("zero1", 0.0), "zero2": fr.get("zero2", 0.0), "seq": fr["seq"], "n": len(c1),
                "seam": fr.get("seam", 0), "valid": fr.get("valid", len(c1))}
        mb = json.dumps(meta, separators=(",", ":")).encode()
        rec = self.MAGIC + struct.pack("<dI", fr["t"], len(mb)) + mb + c1 + c2
        with self.lock:
            now = time.time()
            if (self.fh is None or self.cur["bytes"] + len(rec) > self.SEG_BYTES
                    or (self.cur["t0"] and now - self.cur["t0"] > self.SEG_SECONDS)):
                self._rotate()
            self.fh.write(rec)
            self.cur["bytes"] += len(rec)
            self.cur["frames"] += 1
            self.cur["t0"] = self.cur["t0"] or fr["t"]
            self.cur["t1"] = fr["t"]
            if now - self.last_flush > 2.0:
                self.fh.flush()
                self.last_flush = now

    def status(self):
        with self.lock:
            total = sum(sg["bytes"] for sg in self.segments)
            return {"enabled": self.enabled, "forced": self.forced, "user_forced": self.user_forced,
                    "paused": time.time() < self.paused_until, "root": self.root, "max_gb": self.max_bytes / 1e9,
                    "segments": len(self.segments), "total_mb": total / 1e6,
                    "active": dict(self.cur, active=True) if self.cur else None,
                    "indexer_dropped": (indexer.dropped if indexer else None), "indexer_backlog": (len(indexer.q) if indexer else None),
                    "frames_total": sum(sg["frames"] for sg in self.segments)}

    def list(self):
        with self.lock:
            return [dict(sg, active=(sg is self.cur)) for sg in self.segments]

    def query(self, t_from, t_to, every=1, limit=2000):
        """Frames in [t_from, t_to], every n-th, at most `limit`, as viewer-style dicts."""
        with self.lock:
            segs = [sg for sg in self.segments if sg["t0"] is not None and sg["t0"] <= t_to and (sg["t1"] or time.time()) >= t_from]
            if self.fh:
                self.fh.flush()
        out, k = [], 0
        for sg in segs:
            for t, meta, c1, c2 in self.iter_file(sg["path"], t_from, t_to):
                k += 1
                if (k - 1) % max(1, every):
                    continue
                out.append({"t": t, "hdr": meta["hdr"], "rate": meta["rate"], "vpc1": meta["vpc1"], "vpc2": meta["vpc2"],
                            "zero1": meta.get("zero1", 0.0), "zero2": meta.get("zero2", 0.0), "seq": meta.get("seq"),
                            "valid": meta.get("valid"),
                            "ch1": base64.b64encode(c1).decode(), "ch2": base64.b64encode(c2).decode(), "_raw": (c1, c2)})
                if len(out) >= limit:
                    return out
        return out


class Indexer(threading.Thread):
    """Best-effort per-frame metrics next to each raw segment (<seg>.metrics.jsonl):
    t, per channel amp/mean/rms/freq/clipped/glitches/duty — computed with scope_analysis in
    this thread so the acquisition loop never waits. Backlog beyond 300 frames is dropped
    (the raw record is still there; `scopectl replay` recomputes anything)."""
    FIELDS = ("amp", "mean", "rms", "freq", "clipped", "glitches", "duty", "span")

    def __init__(self, recorder):
        super().__init__(daemon=True)
        self.recorder = recorder
        self.q = []
        self.cv = threading.Condition()
        self.dropped = 0
        self.fh = None
        self.fh_path = None

    def backfill(self):
        """Index segments that have no metrics sidecar (recorded before the indexer existed or
        while it was down). Own thread, paced (~200 frames/s) so live indexing and the Pi stay
        responsive."""
        for sg in list(self.recorder.list()):
            if sg.get("active"):
                continue
            mpath = sg["path"][:-4] + ".metrics.jsonl"
            if os.path.exists(mpath):
                continue
            log("indexer: backfilling", sg["name"])
            n = 0
            with open(mpath + ".tmp", "w") as out:
                for t, meta, c1, c2 in Recorder.iter_file(sg["path"]):
                    row = {"t": round(t, 3), "tb": meta["hdr"].get("tb"), "rng1": meta["hdr"].get("rng1"), "rng2": meta["hdr"].get("rng2"),
                           "ch1": self.metrics(c1, meta["rate"], meta["vpc1"], meta.get("zero1", 0.0)),
                           "ch2": self.metrics(c2, meta["rate"], meta["vpc2"], meta.get("zero2", 0.0))}
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
                    n += 1
                    if n % 50 == 0:
                        time.sleep(0.25)           # paced: ~200 frames/s max, own thread
            os.replace(mpath + ".tmp", mpath)
            log("indexer: backfilled %s (%d frames)" % (sg["name"], n))

    def flush(self):
        try:
            if self.fh:
                self.fh.flush(); os.fsync(self.fh.fileno())
        except Exception:
            pass

    def submit(self, seg_path, t, meta, c1, c2):
        with self.cv:
            if len(self.q) >= 300:
                self.q.pop(0)
                self.dropped += 1
            self.q.append((seg_path, t, meta, c1, c2))
            self.cv.notify()

    @staticmethod
    def metrics(raw, rate, vpc, zero):
        if SA is None:
            return None
        lv = SA.levels(raw, vpc, zero)
        fq = SA.frequency(raw, rate) if lv["span_counts"] >= 6 else None
        pm = SA.pulse_metrics(raw, rate) if lv["span_counts"] >= 6 else None
        return {"amp": round(lv["vamp"], 5), "mean": round(lv["vavg"], 5), "rms": round(lv["vrms_ac"], 5),
                "freq": round(fq, 3) if fq else None, "clipped": int(lv["clipped"]),
                "glitches": pm["glitches"] if pm else 0, "duty": round(pm["duty"], 4) if (pm and pm["duty"] is not None) else None,
                "span": lv["span_counts"]}

    def _backfill_thread(self):
        try:
            self.backfill()
        except Exception as e:
            log("indexer backfill:", e)

    def run(self):
        threading.Thread(target=self._backfill_thread, daemon=True).start()   # never blocks live indexing
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                seg_path, t, meta, c1, c2 = self.q.pop(0)
            try:
                row = {"t": round(t, 3), "tb": meta["hdr"].get("tb"), "rng1": meta["hdr"].get("rng1"), "rng2": meta["hdr"].get("rng2"),
                       "ch1": self.metrics(c1, meta["rate"], meta["vpc1"], meta.get("zero1", 0.0)),
                       "ch2": self.metrics(c2, meta["rate"], meta["vpc2"], meta.get("zero2", 0.0))}
                path = seg_path[:-4] + ".metrics.jsonl"
                if path != self.fh_path:
                    if self.fh:
                        self.fh.close()
                    self.fh = open(path, "a")
                    self.fh_path = path
                self.fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                if (int(t) % 2) == 0:
                    self.fh.flush()
            except Exception as e:
                log("indexer:", e)


def parse_where(expr):
    """'ch2_mean<4.5,ch1_glitches>0' -> list of (ch, field, op, value). ops: < <= > >= == !="""
    conds = []
    for part in (expr or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(ch[12])_(amp|mean|rms|freq|clipped|glitches|duty|span)\s*(<=|>=|==|!=|<|>)\s*(-?[\d.eE+-]+)$", part)
        if not m:
            raise ValueError("bad condition %r (use e.g. ch2_mean<4.5, ch1_glitches>0)" % part)
        conds.append((m.group(1), m.group(2), m.group(3), float(m.group(4))))
    return conds


def row_matches(row, conds):
    for ch, field, op, val in conds:
        m = row.get(ch) or {}
        x = m.get(field)
        if x is None:
            return False
        x = float(x)
        if not {"<": x < val, "<=": x <= val, ">": x > val, ">=": x >= val, "==": x == val, "!=": x != val}[op]:
            return False
    return True


def find_events(recorder, t0, t1, conds, gap=2.0, limit_rows=200000):
    """Scan metrics sidecars in [t0, t1]; cluster matching rows into events (new event after `gap` s)."""
    events, scanned, matched = [], 0, 0
    cur = None
    for sg in recorder.list():
        if sg["t0"] is None or sg["t0"] > t1 or (sg["t1"] or time.time()) < t0:
            continue
        path = sg["path"][:-4] + ".metrics.jsonl"
        try:
            fh = open(path)
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                t = row["t"]
                if t < t0 or t > t1:
                    continue
                scanned += 1
                if scanned > limit_rows:
                    break
                if not row_matches(row, conds):
                    continue
                matched += 1
                if cur and t - cur["last"] <= gap:
                    cur["last"] = t; cur["count"] += 1
                else:
                    cur = {"first": t, "last": t, "count": 1, "sample": row}
                    events.append(cur)
                # keep the most extreme sample per event for the first condition
                if conds:
                    ch, field, op, _ = conds[0]
                    a = (cur["sample"].get(ch) or {}).get(field); b = (row.get(ch) or {}).get(field)
                    if a is not None and b is not None and ((op in ("<", "<=") and b < a) or (op in (">", ">=") and b > a)):
                        cur["sample"] = row
    return {"scanned": scanned, "matched": matched, "events": events}


class RefSignal:
    """The calibration reference on GPIO18 (header pin 12): hardware PWM from the Pi's crystal,
    or a steady level. Only this pin, only these modes. Needs passwordless sudo (pinctrl, sysfs)."""
    C = "/sys/class/pwm/pwmchip0/pwm0"

    def __init__(self):
        self.state = {"mode": "pwm", "hz": 1000.0, "duty": 0.5}

    @staticmethod
    def _sh(cmd):
        return subprocess.run(["sudo", "sh", "-c", cmd], capture_output=True, text=True, timeout=10)

    def set(self, mode, hz=1000.0, duty=0.5):
        if mode == "pwm":
            hz = float(hz); duty = float(duty)
            if not (1.0 <= hz <= 10e6) or not (0.0 < duty < 1.0):
                raise ValueError("hz 1..10e6, duty 0..1")
            period = int(round(1e9 / hz))
            d = max(1, min(period - 1, int(round(period * duty))))
            r = self._sh("pinctrl set 18 a5; C=%s; [ -d $C ] || echo 0 > /sys/class/pwm/pwmchip0/export; sleep 0.1; "
                         "echo 0 > $C/duty_cycle; echo %d > $C/period; echo %d > $C/duty_cycle; echo 1 > $C/enable; cat $C/period"
                         % (self.C, period, d))
            actual = 1e9 / int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
            self.state = {"mode": "pwm", "hz": actual or hz, "duty": d / period}
        elif mode in ("high", "low"):
            self._sh("pinctrl set 18 op %s" % ("dh" if mode == "high" else "dl"))
            self.state = {"mode": mode}
        else:
            raise ValueError("mode pwm|high|low")
        return self.state


class VbusSense:
    """External-power sense for the INTERNAL build: the scope's USB-C VBUS through a divider
    (e.g. 10k over 20k, 5 V -> 3.3 V) to a Pi GPIO. The scope's own `ext` flag cannot be used:
    it is a battery-voltage threshold (> 4.3 V) and reads 0 on USB once the cell is full."""
    def __init__(self, line):
        self.line = line
        self.req = None
        self.err = None
        try:
            import gpiod
            from gpiod.line import Direction, Bias
            self.req = gpiod.request_lines("/dev/gpiochip0", consumer="scope-vbus",
                                           config={line: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_DOWN)})
        except Exception as e:
            self.err = str(e)
            log("vbus sense on GPIO%d unavailable: %s" % (line, e))

    def present(self):
        if self.req is None:
            return None
        try:
            from gpiod.line import Value
            return self.req.get_value(self.line) == Value.ACTIVE
        except Exception as e:
            self.err = str(e)
            return None


class Hub:
    """Acquisition loop + fan-out of the latest frame to web clients."""

    def __init__(self, scope):
        self.scope = scope
        self.cond = threading.Condition()
        self.latest = None
        self.seq = 0
        self.run = True
        self.viewers = 0
        self.fps = 0.0
        self.errors = 0
        self.fast_ok = True
        self.status = "starting"
        self.last_interest = time.time()   # last viewer / frame.json / command
        self.ext_power = True              # policy input: is the PI on external power? (see pi_power)
        self.pi_power = "external"         # "external": Pi has its own supply; "internal": Pi runs off the scope battery
        self.vbus = None                   # VbusSense when internal
        self.scope_ext_hdr = None          # the scope's own (unreliable) ext flag, reported only
        self.mode = "starting"
        self.battery_mv = None
        self.quiesced_idle = False
        self.IDLE_STOP_S = 180
        self.logging = False            # measurement logger: one row per frame, in memory
        self.log_rows = []
        self.log_started = None
        self.LOG_MAX = 200000           # ~2.7 h at 20 fps; oldest rows drop first

    def loop(self):
        times = []
        while True:
            # Nobody watching and not logging: poll once a second for 3 minutes, then stop
            # polling entirely so the scope's own auto power-off / dark sleep can kick in
            # (every shell command counts as activity on the device). A viewer, a
            # frame.json request or a command wakes both ends again.
            # Always-on recording only on EXTERNAL power (or when the user asked with
            # `record start`): on battery an unattended Pi must let the scope auto-off.
            if self.pi_power == "internal":
                v = self.vbus.present() if self.vbus else None
                self.ext_power = bool(v) if v is not None else False
            else:
                self.ext_power = True
            always = recorder is not None and (recorder.user_forced or (recorder.forced and self.ext_power))
            idle = self.viewers == 0 and not self.logging and not always
            stopped = idle and time.time() - self.last_interest > self.IDLE_STOP_S
            if stopped and not self.quiesced_idle and recorder is not None:
                recorder.quiesce("idle (no viewer, on battery or recording off)")
                self.quiesced_idle = True
            if not stopped:
                self.quiesced_idle = False
            self.mode = ("paused (RUN off)" if not self.run else "stopped (idle, scope may auto-off)" if stopped
                         else "idle (1 Hz, no viewer)" if idle else "streaming")
            if not self.run or (idle and self.latest and time.time() - self.latest["t"] < 1.0) or stopped:
                time.sleep(0.05)
                continue
            try:
                try:
                    h, c1, c2 = self.scope.frame(fast=self.fast_ok)
                except ValueError:
                    self.errors += 1
                    h, c1, c2 = self.scope.frame(fast=False)       # paused, always clean
            except ConnectionError as e:
                self.status = "no scope: %s" % e
                time.sleep(1.0)
                continue
            except Exception as e:
                self.errors += 1
                self.status = "error: %s" % e
                time.sleep(0.2)
                continue
            self.status = "ok"
            if "ext" in h:
                self.scope_ext_hdr = bool(h["ext"])
                self.battery_mv = h.get("bat")
            if h.get("off") and recorder is not None and time.time() >= recorder.paused_until:
                recorder.paused_until = time.time() + 15.0      # resumes if power did not drop
                recorder.quiesce("scope power-off notice")
            now = time.time()
            times = [t for t in times if now - t < 3.0] + [now]
            self.fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0
            rate = RATE_OVERRIDE.get(h.get("tb")) or h.get("rate_hz") or TIMEBASE_RATE.get(h.get("tb"), 0)
            # Free-run records hold one ring-buffer seam at a random position. Rotate BOTH
            # channels by the same amount (found on the channel with the larger swing) so the
            # record is continuous for the viewer, the measurements and the recorder; `seam`
            # in the frame says how far (0 = untouched; the firmware's own strobed records are
            # already ordered).
            # (2026-10-11) A free-run record is a rotated ring buffer with a ~24-sample block of
            # junk at a drifting position. Rotate both channels so the continuous part comes
            # first and the junk block (plus the two truncated plateaus around it) sits at the
            # END; `valid` = how many leading samples are continuous. Nothing is discarded: the
            # recorder keeps all samples, measurements use [:valid].
            seam_k, valid = 0, len(c1)
            if SA is not None and not h.get("ordered"):
                try:
                    pick = c1 if (max(c1) - min(c1)) >= (max(c2) - min(c2)) else c2
                    sp = SA.junk_span(pick)
                    if sp:
                        a, b = sp
                        if 0 < b < len(c1):
                            c1 = c1[b:] + c1[:b]
                            c2 = c2[b:] + c2[:b]
                            seam_k = b
                        valid = len(c1) - (b - a)
                except Exception:
                    seam_k, valid = 0, len(c1)
            vpc1 = h.get("vpc1_nv", 0) / 1e9 * calib_gain(1, h.get("rng1"))
            vpc2 = h.get("vpc2_nv", 0) / 1e9 * calib_gain(2, h.get("rng2"))
            z1, z2 = calib_zero(1, h.get("rng1")), calib_zero(2, h.get("rng2"))
            fr = {"seq": self.seq + 1, "t": now, "hdr": h, "rate": rate, "vpc1": vpc1, "vpc2": vpc2,
                  "ch1": base64.b64encode(c1).decode(), "ch2": base64.b64encode(c2).decode(),
                  "zero1": z1, "zero2": z2, "seam": seam_k, "valid": valid,
                  "meas": {"ch1": measure(c1[:valid], vpc1, rate, z1), "ch2": measure(c2[:valid], vpc2, rate, z2)},
                  "fps": round(self.fps, 1), "errors": self.errors}
            if recorder is not None:
                try:
                    recorder.write(fr, c1, c2)
                    if indexer is not None and recorder.cur:
                        indexer.submit(recorder.cur["path"], fr["t"],
                                       {"hdr": h, "rate": rate, "vpc1": vpc1, "vpc2": vpc2, "zero1": z1, "zero2": z2},
                                       c1[:valid], c2[:valid])
                except Exception as e:
                    self.errors += 1
                    self.status = "recorder: %s" % e
            if self.logging:
                m1, m2 = fr["meas"]["ch1"], fr["meas"]["ch2"]
                self.log_rows.append((now, h.get("tb"), h.get("rng1"), h.get("rng2"),
                                      m1.get("vamp"), m1.get("vavg"), m1.get("vrms_ac"), m1.get("freq"),
                                      m2.get("vamp"), m2.get("vavg"), m2.get("vrms_ac"), m2.get("freq")))
                if len(self.log_rows) > self.LOG_MAX:
                    del self.log_rows[:len(self.log_rows) - self.LOG_MAX]
            with self.cond:
                self.seq += 1
                self.latest = fr
                self.cond.notify_all()

    def wait_next(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq, timeout)
            return self.latest


# --------------------------------------------------------------------------------------------
def make_http_handler(hub):
    scope = hub.scope

    class H(BaseHTTPRequestHandler):
        server_version = "scope-server/1"

        def log_message(self, fmt, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p == "/api/info":
                return self._send(200, {
                    "timebases": [{"code": c, "rate": RATE_OVERRIDE.get(c, r), "tier": TIMEBASE_TIER.get(c, "measured"),
                                   "s_per_div": SAMPLES_PER_DIV / RATE_OVERRIDE.get(c, r)} for c, r in sorted(TIMEBASE_RATE.items(), reverse=True)],
                    "mv_per_count": MV_PER_COUNT, "range_tier": RANGE_TIER,
                    "counts_per_div": COUNTS_PER_DIV, "samples_per_div": SAMPLES_PER_DIV, "calib": CALIB,
                    "status": hub.status, "port": scope.port, "run": hub.run})
            if p == "/api/frame.json":
                hub.last_interest = time.time()
                return self._send(200, hub.latest or {})
            if p == "/api/calib":
                return self._send(200, CALIB)
            if p == "/api/refs" or p.startswith("/api/refs/"):
                refdir = os.path.join(sys_path_dir, "refs")
                if p == "/api/refs":
                    out = []
                    for fn in sorted(glob.glob(os.path.join(refdir, "*.json"))):
                        try:
                            with open(fn) as f:
                                r = json.load(f)
                            out.append({"name": r["name"], "t": r["t"], "ch": r["ch"], "note": r.get("note", ""),
                                        "rate": r["rate"], "rng": r["rng"], "vpc": r["vpc"]})
                        except Exception:
                            continue
                    return self._send(200, out)
                name = os.path.basename(p)[:-5] if p.endswith(".json") else os.path.basename(p)
                fn = os.path.join(refdir, re.sub(r"[^\w.-]", "_", name) + ".json")
                if not os.path.exists(fn):
                    return self._send(404, {"error": "no such reference"})
                with open(fn) as f:
                    return self._send(200, json.load(f))
            if p == "/api/refsig":
                return self._send(200, refsig.state)
            if p.startswith("/api/marks"):
                q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1].split("&") if "?" in self.path else []) if "=" in x)
                t0 = float(q.get("from", 0)); t1 = float(q.get("to", time.time() + 1))
                out = []
                try:
                    with open(os.path.join(recorder.root if recorder else sys_path_dir, "markers.jsonl")) as f:
                        for line in f:
                            try:
                                m = json.loads(line)
                            except ValueError:
                                continue
                            if t0 <= m["t"] <= t1:
                                out.append(m)
                except FileNotFoundError:
                    pass
                return self._send(200, out)
            if p == "/api/health":
                st = os.statvfs(recorder.root if recorder else "/")
                fr = hub.latest or {}
                try:
                    with open("/proc/uptime") as f:
                        up = float(f.read().split()[0])
                except Exception:
                    up = None
                try:
                    fw = scope.cmd("version", timeout=3) if scope.ser else ""
                except Exception as e:
                    fw = "error: %s" % e
                rs = recorder.status() if recorder else {}
                return self._send(200, {
                    "time": time.time(), "pi_uptime_s": up, "load": os.getloadavg(),
                    "disk_free_gb": st.f_bavail * st.f_frsize / 1e9, "disk_total_gb": st.f_blocks * st.f_frsize / 1e9,
                    "scope": {"status": hub.status, "port": scope.port, "fps": fr.get("fps"), "errors": hub.errors,
                              "last_frame_age_s": (time.time() - fr["t"]) if fr else None, "firmware": fw.strip().splitlines()[:2]},
                    "recorder": {k: rs.get(k) for k in ("enabled", "forced", "user_forced", "paused", "segments", "total_mb", "frames_total", "indexer_backlog", "indexer_dropped")},
                    "power": {"pi_power": hub.pi_power, "external": hub.ext_power, "battery_mv": hub.battery_mv,
                              "scope_ext_flag": hub.scope_ext_hdr,
                              "vbus_gpio": (hub.vbus.line if hub.vbus else None), "vbus_err": (hub.vbus.err if hub.vbus else None)},
                    "hub_mode": hub.mode,
                    "logging": hub.logging, "viewers": hub.viewers, "calib": CALIB})
            if p == "/api/record":
                return self._send(200, recorder.status() if recorder else {"enabled": False})
            if p == "/api/records":
                return self._send(200, recorder.list() if recorder else [])
            if p.startswith("/api/records/query"):
                q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1].split("&") if "?" in self.path else []) if "=" in x)
                try:
                    t0 = float(q.get("from", time.time() - 600)); t1 = float(q.get("to", time.time()))
                    every = int(q.get("every", 1)); limit = min(int(q.get("limit", 2000)), 20000)
                except ValueError:
                    return self._send(400, {"error": "bad query"})
                frs = recorder.query(t0, t1, every, limit) if recorder else []
                for f in frs:
                    f.pop("_raw", None)
                return self._send(200, {"from": t0, "to": t1, "frames": frs})
            if p.startswith("/api/records/") and p.endswith(".bin"):
                name = os.path.basename(p)
                sg = next((sg for sg in (recorder.list() if recorder else []) if sg["name"] == name), None)
                if not sg:
                    return self._send(404, {"error": "no such segment"})
                with open(sg["path"], "rb") as f:
                    data = f.read()
                return self._send(200, data, "application/octet-stream", {"Content-Disposition": 'attachment; filename="%s"' % name})
            if p.startswith("/api/events"):
                q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1].split("&") if "?" in self.path else []) if "=" in x)
                try:
                    t0 = float(q.get("from", time.time() - 3600)); t1 = float(q.get("to", time.time()))
                    conds = parse_where(urllib_unquote(q.get("where", "")))
                    gap = float(q.get("gap", 2.0))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                if recorder is None:
                    return self._send(503, {"error": "recorder disabled"})
                res = find_events(recorder, t0, t1, conds, gap)
                res.update({"from": t0, "to": t1, "where": q.get("where", ""), "indexer_dropped": indexer.dropped if indexer else None})
                return self._send(200, res)
            if p.startswith("/api/analyze"):
                if SA is None:
                    return self._send(503, {"error": "scope_analysis not available on the Pi: %s" % SA_ERR})
                q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1].split("&") if "?" in self.path else []) if "=" in x)
                try:
                    t0 = float(q.get("from", time.time() - 600)); t1 = float(q.get("to", time.time()))
                    ch = int(q.get("ch", 1)); every = int(q.get("every", 1)); limit = min(int(q.get("limit", 2000)), 20000)
                    pk = {k: float(v) for k, v in q.items() if k in ("hyst", "min_span", "low_res", "glitch_ratio", "freq_spread",
                                                                      "input_limit_v", "limit_fraction", "square_extreme_frac", "sine_crest_max", "tri_crest_max")}
                    params = SA.Params(**pk)
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": "bad query: %s" % e})
                frs = recorder.query(t0, t1, every, limit) if recorder else []
                if not frs:
                    return self._send(404, {"error": "no stored frames in that window"})
                recs, rows = [], []
                for f in frs:
                    c1, c2 = f.pop("_raw")
                    raw = c1 if ch == 1 else c2
                    raw = raw[:f["valid"]] if f.get("valid") else SA.clean_record(raw)
                    h = f["hdr"]
                    r = {"raw": raw, "rate": f["rate"], "vpc": f["vpc%d" % ch], "zero": f.get("zero%d" % ch, 0.0),
                         "probe": 10 if h.get("prb%d" % ch) else 1, "cpl": ["DC", "AC", "GND"][h.get("cpl%d" % ch, 0)],
                         "rng": h.get("rng%d" % ch), "tier": RANGE_TIER[h.get("rng%d" % ch, 0)], "enabled": bool(h.get("en%d" % ch, 1)),
                         "channel": ch, "counts_per_div": COUNTS_PER_DIV, "gain": calib_gain(ch, h.get("rng%d" % ch)), "t": f["t"], "clean": True}
                    recs.append(r)
                    lv = SA.levels(raw, r["vpc"], r["zero"]); fq = SA.frequency(raw, r["rate"], params); pm = SA.pulse_metrics(raw, r["rate"], params)
                    rows.append({"t": f["t"], "amp": lv["vamp"], "mean": lv["vavg"], "rms_ac": lv["vrms_ac"], "freq": fq, "clipped": lv["clipped"],
                                 "glitches": pm["glitches"] if pm else 0, "duty": pm["duty"] if pm else None})
                overall = SA.analyze_records(recs, params)
                return self._send(200, {"from": t0, "to": t1, "ch": ch, "frames": len(recs), "overall": overall, "per_frame": rows})
            if p == "/api/log":
                return self._send(200, {"logging": hub.logging, "rows": len(hub.log_rows),
                                        "since": hub.log_started})
            if p == "/api/log.csv":
                rows = ["t_unix,t_iso,tb,rng1,rng2,ch1_amp,ch1_mean,ch1_rms_ac,ch1_freq,ch2_amp,ch2_mean,ch2_rms_ac,ch2_freq"]
                for r in list(hub.log_rows):
                    rows.append(",".join([f"{r[0]:.3f}", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r[0]))]
                                         + ["" if v is None else (f"{v:.6g}" if isinstance(v, float) else str(v)) for v in r[1:]]))
                name = time.strftime("scope_log_%Y%m%d_%H%M%S.csv")
                return self._send(200, "\n".join(rows) + "\n", "text/csv",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if p == "/api/screen.png":
                q = dict(x.split("=", 1) for x in self.path.split("?")[1:2][0].split("&")) if "?" in self.path else {}
                try:
                    scale = max(1, min(4, int(q.get("scale", "2"))))
                except ValueError:
                    scale = 2
                # hold=1 (default): the firmware dumps the LCD in pages while the live
                # compositor keeps drawing, so a free-running trace is stitched from two
                # frames. Parking the trigger in SINGLE holds one record for the capture;
                # the previous mode is restored afterwards.
                hold = q.get("hold", "1") != "0"
                prev = None
                try:
                    if hold:
                        fr = hub.latest
                        prev = ["auto", "normal", "single"][fr["hdr"].get("tmode", 0)] if fr else "auto"
                        if prev != "single":
                            scope.cmd("fpga scope trigmode single")
                            time.sleep(0.5)
                    w, h, rows = scope.screen()
                except (ConnectionError, ValueError) as e:
                    return self._send(503, {"error": str(e)})
                finally:
                    if hold and prev and prev != "single":
                        try:
                            scope.cmd("fpga scope trigmode " + prev)
                        except (ConnectionError, ValueError):
                            pass
                name = time.strftime("2c53t_screen_%Y%m%d_%H%M%S.png")
                return self._send(200, png_rgb(w, h, rows, scale), "image/png",
                                  {"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})
            if p == "/api/frame.csv":
                fr = hub.latest
                if not fr:
                    return self._send(503, "no frame yet\n", "text/plain")
                c1, c2 = base64.b64decode(fr["ch1"]), base64.b64decode(fr["ch2"])
                rate, t0 = fr["rate"], (-512 if fr["hdr"].get("ordered") else 0)
                rows = ["i,t_s,ch1_raw,ch2_raw,ch1_V,ch2_V"]
                for i in range(len(c1)):
                    t = (i + t0) / rate if rate else ""
                    v1 = (c1[i] - 128) * fr["vpc1"] if fr["vpc1"] else ""
                    v2 = (c2[i] - 128) * fr["vpc2"] if fr["vpc2"] else ""
                    rows.append(f"{i},{t},{c1[i]},{c2[i]},{v1},{v2}")
                name = time.strftime("scope_%Y%m%d_%H%M%S.csv", time.localtime(fr["t"]))
                return self._send(200, "\n".join(rows) + "\n", "text/csv",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if p == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                hub.viewers += 1
                hub.last_interest = time.time()
                last = -1
                try:
                    while True:
                        hub.last_interest = time.time()
                        fr = hub.wait_next(last, 5.0)
                        if fr and fr["seq"] != last:
                            last = fr["seq"]
                            self.wfile.write(b"data: " + json.dumps(fr).encode() + b"\n\n")
                        else:
                            self.wfile.write(b": keepalive " + hub.status.encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    hub.viewers -= 1
                return
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if self.path == "/api/mark":
                # {"text": "spindle on", "t": optional unix time} -> markers.jsonl next to the records
                text = str(req.get("text", "")).strip()[:200]
                if not text:
                    return self._send(400, {"error": "need text"})
                m = {"t": float(req.get("t", time.time())), "text": text}
                with open(os.path.join(recorder.root if recorder else sys_path_dir, "markers.jsonl"), "a") as f:
                    f.write(json.dumps(m) + "\n")
                return self._send(200, m)
            if self.path == "/api/refsig":
                try:
                    st = refsig.set(req.get("mode", "pwm"), req.get("hz", 1000.0), req.get("duty", 0.5))
                except (ValueError, subprocess.SubprocessError) as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, st)
            if self.path == "/api/refs":
                # {"name": "...", "ch": 1|2, "note": "..."} -> store the latest record of that channel
                fr = hub.latest
                if not fr:
                    return self._send(503, {"error": "no frame yet"})
                try:
                    ch = int(req.get("ch", 1)); name = re.sub(r"[^\w.-]", "_", str(req["name"]))
                except (KeyError, ValueError):
                    return self._send(400, {"error": "need name and ch"})
                refdir = os.path.join(sys_path_dir, "refs")
                os.makedirs(refdir, exist_ok=True)
                h = fr["hdr"]
                rec = {"name": name, "t": fr["t"], "ch": ch, "note": str(req.get("note", "")), "rate": fr["rate"],
                       "vpc": fr["vpc%d" % ch], "zero": fr.get("zero%d" % ch, 0.0), "rng": h["rng%d" % ch],
                       "probe": 10 if h.get("prb%d" % ch) else 1, "cpl": h.get("cpl%d" % ch), "tb": h["tb"],
                       "raw": fr["ch%d" % ch]}
                with open(os.path.join(refdir, name + ".json"), "w") as f:
                    json.dump(rec, f)
                return self._send(200, {k: v for k, v in rec.items() if k != "raw"})
            if self.path == "/api/record":
                if recorder is None:
                    return self._send(503, {"error": "recorder disabled"})
                a = req.get("action")
                if a == "start":
                    recorder.enabled = True; recorder.user_forced = True; hub.run = True
                elif a == "stop":
                    recorder.user_forced = False
                elif a == "disable":
                    recorder.enabled = False; recorder.user_forced = False
                if req.get("max_gb"):
                    recorder.max_bytes = int(float(req["max_gb"]) * 1e9)
                return self._send(200, recorder.status())
            if self.path == "/api/calib":
                # {"ch": 2, "rng": 6, "gain": 1.062} and/or {"ch": 2, "rng": 6, "zero": 16.4}; "clear": true
                try:
                    ch, rng = str(int(req["ch"])), str(int(req["rng"]))
                except (KeyError, ValueError):
                    return self._send(400, {"error": "need ch and rng"})
                if req.get("clear"):
                    CALIB["gain"].get(ch, {}).pop(rng, None)
                    CALIB["zero"].get(ch, {}).pop(rng, None)
                if "gain" in req:
                    CALIB["gain"].setdefault(ch, {})[rng] = float(req["gain"])
                if "zero" in req:
                    CALIB["zero"].setdefault(ch, {})[rng] = float(req["zero"])
                calib_save()
                return self._send(200, CALIB)
            if self.path == "/api/log":
                a = req.get("action")
                if a == "start":
                    hub.log_rows = []
                    hub.log_started = time.time()
                    hub.logging = True
                    hub.run = True
                elif a == "stop":
                    hub.logging = False
                elif a == "clear":
                    hub.log_rows = []
                return self._send(200, {"logging": hub.logging, "rows": len(hub.log_rows)})
            if self.path == "/api/run":
                hub.run = bool(req.get("run", True))
                return self._send(200, {"run": hub.run})
            if self.path == "/api/cmd":
                c = str(req.get("cmd", "")).strip()
                if not any(r.match(c) for r in ALLOWED):
                    return self._send(403, {"ok": False, "out": f"not allowed from the web UI: {c!r}"})
                try:
                    return self._send(200, {"ok": True, "out": scope.cmd(c)})
                except Exception as e:
                    return self._send(502, {"ok": False, "out": str(e)})
            return self._send(404, {"error": "not found"})

    return H


class TcpShell(socketserver.BaseRequestHandler):
    """Line-based raw shell proxy (compat with the old socat bridge on :7001)."""
    scope = None

    def handle(self):
        sock = self.request
        buf = b""
        try:
            while True:
                d = sock.recv(4096)
                if not d:
                    return
                buf += d
                while re.search(rb"[\r\n]", buf):
                    line, buf = re.split(rb"[\r\n]", buf, 1)
                    line = line.strip().decode(errors="replace")
                    if not line:
                        sock.sendall(b"\r\n> ")
                        continue
                    try:
                        sock.sendall(self.scope.raw(line, 5.0))
                    except ConnectionError as e:
                        sock.sendall(f"ERR: {e}\r\n> ".encode())
        except OSError:
            return


class ThreadingTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


recorder = None
indexer = None
refsig = RefSignal()


def urllib_unquote(x):
    import urllib.parse
    return urllib.parse.unquote_plus(x)


def main():
    global recorder
    calib_load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=8080)
    ap.add_argument("--tcp", type=int, default=7001)
    ap.add_argument("--records", default=os.path.expanduser("~/records"), help="raw recorder folder ('' = disabled)")
    ap.add_argument("--max-gb", type=float, default=40.0, help="ring size for raw records (54 GB free on the Pi, 2026-10-11)")
    ap.add_argument("--record-always", type=int, default=1, help="1: keep polling+recording with no viewer while on external power")
    ap.add_argument("--pi-power", choices=["external", "internal"], default="external",
                    help="internal = Pi runs off the scope battery (always-recording only while VBUS is sensed)")
    ap.add_argument("--vbus-gpio", type=int, default=17, help="GPIO with the scope's VBUS via a divider (internal build)")
    a = ap.parse_args()
    global indexer
    if a.records:
        recorder = Recorder(a.records, int(a.max_gb * 1e9))
        recorder.forced = bool(a.record_always)
        indexer = Indexer(recorder)
        indexer.start()
        st = recorder.status()
        log("recorder: %s, %d segments, %.1f MB, ring %.0f GB, always=%s" % (a.records, st["segments"], st["total_mb"], a.max_gb, recorder.forced))
    scope = Scope()
    hub = Hub(scope)
    hub.pi_power = a.pi_power
    if a.pi_power == "internal":
        hub.vbus = VbusSense(a.vbus_gpio)
    log("power: Pi on %s supply%s" % (a.pi_power, (", VBUS sense GPIO%d" % a.vbus_gpio) if a.pi_power == "internal" else ""))
    threading.Thread(target=hub.loop, daemon=True).start()
    TcpShell.scope = scope
    tcp = ThreadingTCP(("0.0.0.0", a.tcp), TcpShell)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    http = ThreadingHTTPServer(("0.0.0.0", a.http), make_http_handler(hub))
    http.daemon_threads = True
    log(f"viewer on :{a.http}, raw shell on :{a.tcp}")
    http.serve_forever()


if __name__ == "__main__":
    main()
