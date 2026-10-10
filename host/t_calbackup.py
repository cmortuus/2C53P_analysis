#!/usr/bin/env python3
"""Back up the 2C53T's factory calibration page (MCU flash 0x08006000..0x08006FFF, 4 KB)
through the OpenScope debug shell, and verify it against the CRC the caldump image shows.

    t_calbackup.py --port /dev/cu.usbmodem* [--out cal_page_<date>.bin] [--expect-crc 1A2B3C4D]
    t_calbackup.py --tcp 10.1.13.114:7001 ...

Reads 64 words per `mem read` call (the shell's cap), 16 calls, checks every word was returned,
writes the binary, prints CRC32 (same algorithm as the caldump screen: poly 0xEDB88320, init
0xFFFFFFFF, final XOR). Refuses to overwrite an existing output file. Never writes to the scope.
"""
import argparse, re, sys, time, zlib, datetime, os
sys.path.insert(0, os.path.dirname(__file__))
from t_scope import Scope

BASE, LEN = 0x08006000, 0x1000
LINE = re.compile(rb"0x([0-9A-F]{8}):((?: [0-9A-F]{8}){1,4})")


def read_region(sc, base, length, words_per_call=64, retries=3):
    data = bytearray(); addr = base
    while addr < base + length:
        n = min(words_per_call, (base + length - addr) // 4)
        for attempt in range(retries):
            raw = sc.cmd(f"mem read 0x{addr:08X} {n}", wait=0.6).encode(errors='ignore')
            words = {}
            for m in LINE.finditer(raw):
                a = int(m.group(1), 16)
                for i, w in enumerate(m.group(2).split()):
                    words[a + 4 * i] = int(w, 16)
            chunk = [words.get(addr + 4 * i) for i in range(n)]
            if all(w is not None for w in chunk):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(f"incomplete read at 0x{addr:08X} after {retries} attempts")
        for w in chunk:
            data += w.to_bytes(4, 'little')
        addr += 4 * n
        print(f"\r  {len(data)}/{length} bytes", end='', file=sys.stderr)
    print(file=sys.stderr)
    return bytes(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port'); ap.add_argument('--tcp')
    ap.add_argument('--out', default=f"cal_page_0x08006000_{datetime.date.today().isoformat()}.bin")
    ap.add_argument('--expect-crc', help='CRC32 shown by the caldump image (hex)')
    a = ap.parse_args()
    if not a.port and not a.tcp: ap.error('need --port or --tcp')
    if os.path.exists(a.out): sys.exit(f"refusing to overwrite {a.out}")
    sc = Scope(a.port, tcp=a.tcp)
    v = sc.cmd('version', wait=0.5).strip()
    print('shell:', v.splitlines()[0] if v else '(no banner — is the OpenScope image running?)', file=sys.stderr)
    data = read_region(sc, BASE, LEN)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    open(a.out, 'wb').write(data)
    blank = data.count(0xFF) == len(data)
    print(f"wrote {a.out}: {len(data)} bytes, CRC32 {crc:08X}{'  (ALL 0xFF — page is blank!)' if blank else ''}")
    if a.expect_crc:
        ok = int(a.expect_crc, 16) == crc
        print('CRC matches caldump screen' if ok else f"CRC MISMATCH: expected {a.expect_crc.upper()} — do NOT trust this backup")
        sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
