#!/usr/bin/env python3
"""Host side for the OpenScope-2C53T `scope dumpbin` command: live waveform streaming,
CSV logging and settings control over the USB CDC debug shell.

    t_scope.py --port /dev/cu.usbmodem*            # live plot (matplotlib), ~20 fps
    t_scope.py --port ... --csv run.csv --frames 50
    t_scope.py --port ... --set "fpga scope timebase 0x10" --set "fpga scope vdiv 1 7"
    t_scope.py --port ... --serve 0.0.0.0:7001     # Pi mode: broadcast frames as JSON lines

Frame = header line `SCOPEBIN k=v ... len=N crc32=XXXXXXXX\\r\\n` + N raw bytes (CH1 then CH2,
unsigned 8-bit ADC codes) + `\\r\\nSCOPEBIN END\\r\\n`. The header carries integer units
(rate_hz samples/s, vpcN_nv nanovolts/count; 0 = uncalibrated). Volts = (code - 128) * vpc
(vpc = 0 -> plot counts). Time = (i - 512) * sps when the record is
`ordered` (hardware trigger at index 512), else (i) * sps free-running.
"""
import argparse, json, re, socket, sys, threading, time, zlib

HDR = re.compile(rb"SCOPEBIN ((?:\w+=[\w.+-]+ ?)+)\r\n")
END = b"\r\nSCOPEBIN END\r\n"


def parse_header(m):
    kv = {}
    for tok in m.group(1).decode().split():
        k, v = tok.split('=', 1)
        if k == 'crc32':
            kv[k] = int(v, 16)
        else:
            try:
                kv[k] = int(v, 0)
            except ValueError:
                kv[k] = float(v)
    # derived SI values (firmware sends integer units: samples/s, nanovolts; 0 = unknown)
    kv['sps'] = 1.0 / kv['rate_hz'] if kv.get('rate_hz') else 0.0
    kv['vpc1'] = kv.get('vpc1_nv', 0) / 1e9
    kv['vpc2'] = kv.get('vpc2_nv', 0) / 1e9
    return kv


class _TcpPort:
    """Minimal pyserial-like wrapper over a TCP socket (ESP32-S3 bridge, esp32_bridge/)."""
    def __init__(self, hostport, timeout=0.3):
        host, port = hostport.rsplit(':', 1)
        self.sock = socket.create_connection((host, int(port))); self.sock.settimeout(timeout)
    def write(self, b): self.sock.sendall(b)
    def read(self, n):
        try: return self.sock.recv(n)
        except socket.timeout: return b''
    def reset_input_buffer(self):
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(65536): pass
        except (socket.timeout, OSError): pass
        self.sock.settimeout(0.3)


class Scope:
    def __init__(self, port, baud=115200, tcp=None):
        if tcp:
            self.s = _TcpPort(tcp)
        else:
            import serial
            self.s = serial.Serial(port, baud, timeout=0.3)
        self.s.reset_input_buffer(); self.s.write(b"\r\n"); time.sleep(0.2); self.s.reset_input_buffer()

    def cmd(self, line, wait=0.3):
        """Send a shell command, return its text response."""
        self.s.reset_input_buffer(); self.s.write(line.encode() + b"\r\n")
        time.sleep(wait); return self.s.read(65536).decode(errors='replace')

    def frame(self, timeout=2.0):
        """Request one capture record. Returns (header dict, ch1 bytes, ch2 bytes)."""
        self.s.reset_input_buffer(); self.s.write(b"scope dumpbin\r\n")
        buf, t0, m = b"", time.time(), None
        while time.time() - t0 < timeout:
            buf += self.s.read(4096)
            m = HDR.search(buf)
            if m: break
            if b"ERR:" in buf: raise RuntimeError(buf.decode(errors='replace').strip())
        if not m: raise TimeoutError("no SCOPEBIN header")
        h = parse_header(m); n = h['len']
        data = buf[m.end():]
        while len(data) < n and time.time() - t0 < timeout:
            data += self.s.read(n - len(data))
        data = data[:n]
        if len(data) < n: raise TimeoutError(f"short record {len(data)}/{n}")
        if zlib.crc32(data) & 0xFFFFFFFF != h['crc32']: raise ValueError("CRC mismatch")
        half = h['n']
        return h, data[:half], data[half:2 * half]


def to_units(h, ch1, ch2):
    n = h['n']; sps = h.get('sps', 0.0) or 0.0
    t0 = -512 if h.get('ordered') else 0
    t = [(i + t0) * sps if sps else i for i in range(n)]
    def volts(raw, vpc):
        return [(c - 128) * vpc for c in raw] if vpc else list(raw)
    return t, volts(ch1, h.get('vpc1', 0.0)), volts(ch2, h.get('vpc2', 0.0))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', help='serial port (USB CDC)')
    ap.add_argument('--tcp', help='host:port of the esp32_bridge instead of --port')
    ap.add_argument('--set', action='append', default=[], help='shell command to send first (repeatable)')
    ap.add_argument('--csv'); ap.add_argument('--frames', type=int, default=0, help='stop after N frames (0 = run)')
    ap.add_argument('--plot', action='store_true'); ap.add_argument('--serve', help='host:port TCP JSON-lines broadcast')
    ap.add_argument('--interval', type=float, default=0.05)
    a = ap.parse_args()
    if not a.port and not a.tcp:
        ap.error('need --port or --tcp')
    sc = Scope(a.port, tcp=a.tcp)
    for c in a.set:
        print(f'> {c}\n{sc.cmd(c).strip()}')
    clients = []
    if a.serve:
        host, port = a.serve.rsplit(':', 1); srv = socket.create_server((host, int(port)))
        def acc():
            while True:
                c, _ = srv.accept(); clients.append(c)
        threading.Thread(target=acc, daemon=True).start(); print(f'serving on {a.serve}', file=sys.stderr)
    csv = open(a.csv, 'w') if a.csv else None
    if csv: csv.write('frame,gen,i,t_s,ch1_raw,ch2_raw,ch1_V,ch2_V\n')
    plot = None
    if a.plot:
        import matplotlib.pyplot as plt
        plt.ion(); fig, ax = plt.subplots(); l1, = ax.plot([], [], 'c', label='CH1'); l2, = ax.plot([], [], 'b', label='CH2')
        ax.legend(); ax.grid(True); plot = (plt, fig, ax, l1, l2)
    k, last_gen = 0, None
    while not a.frames or k < a.frames:
        try:
            h, c1, c2 = sc.frame()
        except (TimeoutError, ValueError, RuntimeError) as e:
            print('frame error:', e, file=sys.stderr); time.sleep(0.2); continue
        if h['gen'] == last_gen:
            time.sleep(a.interval); continue      # no new capture yet
        last_gen = h['gen']; k += 1
        t, v1, v2 = to_units(h, c1, c2)
        print(f"#{k} gen={h['gen']} ready={h.get('ready')} ordered={h.get('ordered')} tb=0x{h['tb']:02X} "
              f"sps={h.get('sps')} rng=({h.get('rng1')},{h.get('rng2')}) vpc=({h.get('vpc1')},{h.get('vpc2')}) "
              f"ch1 {min(c1)}..{max(c1)} ch2 {min(c2)}..{max(c2)}", file=sys.stderr)
        if csv:
            for i in range(h['n']):
                csv.write(f"{k},{h['gen']},{i},{t[i]:.9g},{c1[i]},{c2[i]},{v1[i]:.6g},{v2[i]:.6g}\n")
            csv.flush()
        if clients:
            line = (json.dumps({'type': 'frame', 'hdr': h, 'ch1': c1.hex(), 'ch2': c2.hex()}) + '\n').encode()
            for c in list(clients):
                try: c.sendall(line)
                except OSError: clients.remove(c)
        if plot:
            plt, fig, ax, l1, l2 = plot
            l1.set_data(t, v1); l2.set_data(t, v2); ax.relim(); ax.autoscale_view()
            ax.set_xlabel('t [s]' if h.get('sps') else 'sample'); ax.set_ylabel('V' if h.get('vpc1') or h.get('vpc2') else 'ADC code')
            fig.canvas.draw_idle(); plt.pause(0.001)
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
