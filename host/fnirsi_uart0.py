#!/usr/bin/env python3
"""FNIRSI 2C53P UART0 ("factory jig") protocol — decoder, ACK/answer engine and CLI monitor.

Derived from APP V1.14 (uart0_cmd_responder @0x23178, UART0_IRQHandler @0x2E0A0); the
code is byte-identical in V1.8 and V1.10. Link: 921600 8N1, 3.3 V, on the main board's
7-pin connector (BUZZ_CHK 3V3 GND 5V TX RX 3V3_EN) / the TX,RX pads under the MCU.

The link is DEVICE-DRIVEN. The scope pulls 16-bit items {hdr, cmd} from a FreeRTOS queue
(filled by the UI on every settings change, 22 items at boot) and for each one sends

    hdr  cmd  data[n]  cksum  0x0A        cksum = (hdr + cmd + sum(data)) & 0xFF

    hdr 0x55  "notification": carries the current value; the host must ACK by echoing the
              frame back verbatim.  Until it gets the echo the scope resends every ~100 polls.
    hdr 0xAA  "query": the host answers with   X cmd value[...] cksum 0x0A   (X = any byte,
              cksum = sum of all bytes before it) and the scope APPLIES value as a SET.
              This is the only host->scope control path in stock firmware.

Command numbers (data = 1 byte unless noted); names marked ? are not yet confirmed:
"""
import sys, time, argparse, json, threading

BAUD = 921600
HDR_NOTIFY, HDR_QUERY = 0x55, 0xAA

# cmd -> (name, nbytes) ; values are indices/raw bytes, see HANDOFF.md for the g_settings map
COMMANDS = {
    0x01: ('settings_00?', 1), 0x02: ('timebase_idx?', 1),
    0x03: ('ch1_vdiv_idx', 1), 0x04: ('ch2_vdiv_idx', 1),
    0x05: ('ch1_pos', 1), 0x06: ('ch2_pos', 1),
    0x07: ('settings_06?', 1), 0x08: ('settings_07?', 1), 0x09: ('settings_0A?', 1),
    0x0B: ('settings_0B?', 1), 0x0C: ('settings_0C?', 1),
    0x0D: ('ch_mask', 1), 0x0E: ('ch_mask', 1),
    0x0F: ('settings_14?', 1), 0x10: ('settings_15?', 1), 0x12: ('settings_16?', 1),
    0x13: ('settings_19_trig?', 1),
    0x1A: ('boot_id', 2), 0x1C: ('settings_14f?', 1), 0x1D: ('u32_150?', 4),
    0x1E: ('settings_154?', 1), 0x1F: ('settings_155?', 1), 0x20: ('float_156?', 1),
    0x21: ('jig_handshake', 0),
    0x54: ('u16_136', 2), 0x55: ('u16_138', 2),
}
for _c in range(0x28, 0x3C):
    COMMANDS[_c] = (f'cal_{_c - 0x28:02d}', 1)   # bytes at 0x200001B8.. (calibration)


def checksum(b):
    return sum(b) & 0xFF


def build(hdr, cmd, data=b''):
    """Build a frame the way the scope does (and the way it expects answers)."""
    body = bytes([hdr, cmd]) + bytes(data)
    return body + bytes([checksum(body), 0x0A])


def parse(buf):
    """Split a byte buffer into frames; returns (frames, remainder). Frames end in 0x0A and
    must checksum; the data length is taken from the command table when known."""
    frames, i = [], 0
    while i < len(buf):
        if buf[i] not in (HDR_NOTIFY, HDR_QUERY):
            i += 1; continue
        if i + 1 >= len(buf):
            break
        cmd = buf[i + 1]
        n = COMMANDS.get(cmd, (None, None))[1]
        cands = [n] if n is not None else [0, 1, 2, 4]
        hit = None
        for k in cands:
            end = i + 2 + k + 2
            if end > len(buf):
                continue
            f = buf[i:end]
            if f[-1] == 0x0A and f[-2] == checksum(f[:-2]):
                hit = f; break
        if hit is None:
            if i + 2 + max(cands) + 2 > len(buf):
                break          # wait for more bytes
            i += 1; continue   # garbage, resync
        frames.append(hit); i += len(hit)
    return frames, buf[i:]


def describe(f):
    hdr, cmd, data = f[0], f[1], f[2:-2]
    name = COMMANDS.get(cmd, ('?', None))[0]
    val = int.from_bytes(data, 'big') if data else None
    return {'t': round(time.time(), 3), 'hdr': 'notify' if hdr == HDR_NOTIFY else 'query',
            'cmd': cmd, 'name': name, 'value': val, 'raw': f.hex()}


class Engine:
    """Stateful protocol handler: ACKs notifications, answers queries, tracks last values.
    `answers` maps cmd -> bytes to reply with when the scope queries that cmd; by default a
    query is answered with the last value the scope itself reported (= no change)."""

    def __init__(self, write, on_event=None, answer_unknown=False):
        self.write, self.on_event = write, on_event or (lambda e: None)
        self.state, self.answers, self.rx = {}, {}, b''
        self.answer_unknown = answer_unknown   # answer queries we have no value for (with 0)
        self.lock = threading.Lock()

    def feed(self, data):
        self.rx += data
        frames, self.rx = parse(self.rx)
        for f in frames:
            ev = describe(f)
            hdr, cmd, payload = f[0], f[1], f[2:-2]
            with self.lock:
                if hdr == HDR_NOTIFY:
                    self.state[cmd] = payload
                    self.write(bytes(f))                      # ACK = echo
                    ev['action'] = 'acked'
                else:
                    ans = self.answers.pop(cmd, None)
                    if ans is not None:
                        ev['action'] = f'answered({bytes(ans).hex()})'
                    elif cmd in self.state:
                        ans = self.state[cmd]; ev['action'] = 'answered(unchanged)'
                    elif self.answer_unknown:
                        ans = payload if payload else b'\x00'; ev['action'] = 'answered(default)'
                    else:
                        ev['action'] = 'unanswered (scope will re-ask ~1/s; use --answer)'
                    if ans is not None:
                        self.write(build(HDR_QUERY, cmd, ans))
            self.on_event(ev)

    def set_answer(self, cmd, value_bytes):
        with self.lock:
            self.answers[cmd] = bytes(value_bytes)


def open_serial(port):
    import serial
    return serial.Serial(port, BAUD, timeout=0.05)


def main():
    ap = argparse.ArgumentParser(description='Monitor/ACK the 2C53P UART0 link')
    ap.add_argument('--port', required=True, help='e.g. /dev/ttyAMA0, /dev/ttyUSB0, /dev/cu.usbserial-*')
    ap.add_argument('--no-ack', action='store_true', help='listen only (scope will keep resending)')
    ap.add_argument('--log', help='append JSON lines here')
    ap.add_argument('--answer', action='append', default=[], metavar='CMD=VALUE',
                    help='reply VALUE when the scope queries CMD (hex ok), e.g. --answer 0x21=0x64 to pose as the factory jig')
    ap.add_argument('--answer-unknown', action='store_true', help='answer queries with no known value using 0')
    a = ap.parse_args()
    ser = open_serial(a.port)
    log = open(a.log, 'a') if a.log else None
    def emit(ev):
        line = json.dumps(ev); print(line, flush=True)
        if log: log.write(line + '\n'); log.flush()
    eng = Engine((lambda b: None) if a.no_ack else ser.write, emit, answer_unknown=a.answer_unknown)
    for spec in a.answer:
        c, v = spec.split('='); eng.set_answer(int(c, 0), int(v, 0).to_bytes(COMMANDS.get(int(c, 0), ('', 1))[1] or 1, 'big'))
    print(f'# listening on {a.port} @ {BAUD}; power-cycle the scope to see the boot burst', file=sys.stderr)
    raw = open(a.log + '.raw', 'ab') if a.log else None
    while True:
        d = ser.read(64)
        if d:
            if raw: raw.write(d); raw.flush()
            eng.feed(d)


if __name__ == '__main__':
    main()
