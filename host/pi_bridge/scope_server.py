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
           POST /api/cmd          {"cmd": "<shell command>"}   (allow-listed scope controls only)
           POST /api/run          {"run": true|false}           (pause/resume streaming)
  :7001  raw shell proxy, line based — drop-in for the old socat bridge, so
         `host/t_scope.py --tcp scope-pi.local:7001` keeps working. Commands from
         TCP clients and from the acquisition loop are serialised on one lock.

Only the standard library + pyserial. Run:  python3 scope_server.py [--http 8080] [--tcp 7001]
"""
import argparse, base64, glob, json, math, os, re, socket, socketserver, subprocess, threading, time, zlib
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
TIMEBASE_RATE = {0x0D: 123662.7, 0x0E: 49930.1, 0x0F: 24979.1, 0x10: 12490.0,
                 0x11: 4990.8, 0x12: 2494.9, 0x13: 1250.4, 0x14: 500.2}
TIMEBASE_TIER = {0x0D: "provisional"}                      # others in the dict: "measured"
SAMPLES_PER_DIV = 32                                        # firmware renderer convention
COUNTS_PER_DIV = 32
MV_PER_COUNT = {1: [0, 0, 0, 0, 14.08, 21.83, 42.95, 88.42, 279.05, 352.17],
                2: [0, 0, 0, 0, 9.49, 20.96, 41.71, 83.79, 223.71, 425.00]}
RANGE_TIER = ["none"] * 4 + ["provisional", "measured", "measured", "measured", "provisional", "provisional"]

# Commands the web UI may send. Anything else (mem write, flash, fwload…) is refused over HTTP.
ALLOWED = [re.compile(p) for p in (
    r"^fpga scope (timebase|vdiv|level|trigmode|edge|graticule|softtrig|hpos|measure|freq|cal)( [\w.\-]+){0,2}$",
    r"^idle( off| \d{1,3})?$",   # not `center`: it stalls the CDC shell for seconds (link drop, 2026-10-10)
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


def measure(raw, vpc, rate):
    """Basic measurements on one channel. Volts when vpc>0, else ADC counts."""
    n = len(raw)
    if n == 0:
        return {}
    mn, mx = min(raw), max(raw)
    mean = sum(raw) / n
    var = sum((x - mean) ** 2 for x in raw) / n
    unit = "V" if vpc else "cnt"
    s = vpc if vpc else 1.0
    zero = 128 if vpc else 0
    # Top/base = median of the samples above/below the mean: a square wave's flat levels,
    # immune to noise peaks and overshoot (max-min over-reads by 2-3 counts on coarse ranges).
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
            idle = self.viewers == 0 and not self.logging
            if idle and self.viewers == 0:
                pass
            if not self.run or (idle and self.latest and time.time() - self.latest["t"] < 1.0) \
               or (idle and time.time() - self.last_interest > self.IDLE_STOP_S):
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
            now = time.time()
            times = [t for t in times if now - t < 3.0] + [now]
            self.fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0
            rate = RATE_OVERRIDE.get(h.get("tb")) or h.get("rate_hz") or TIMEBASE_RATE.get(h.get("tb"), 0)
            vpc1, vpc2 = h.get("vpc1_nv", 0) / 1e9, h.get("vpc2_nv", 0) / 1e9
            fr = {"seq": self.seq + 1, "t": now, "hdr": h, "rate": rate, "vpc1": vpc1, "vpc2": vpc2,
                  "ch1": base64.b64encode(c1).decode(), "ch2": base64.b64encode(c2).decode(),
                  "meas": {"ch1": measure(c1, vpc1, rate), "ch2": measure(c2, vpc2, rate)},
                  "fps": round(self.fps, 1), "errors": self.errors}
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
                    "counts_per_div": COUNTS_PER_DIV, "samples_per_div": SAMPLES_PER_DIV,
                    "status": hub.status, "port": scope.port, "run": hub.run})
            if p == "/api/frame.json":
                hub.last_interest = time.time()
                return self._send(200, hub.latest or {})
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=8080)
    ap.add_argument("--tcp", type=int, default=7001)
    a = ap.parse_args()
    scope = Scope()
    hub = Hub(scope)
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
