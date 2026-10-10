#!/usr/bin/env python3
"""
FNIRSI 2C53P app-firmware inspector.

Usage: python3 fw_inspect.py <2C53P_APP_*.bin> [--dump-rw out.bin]

- Prints the Cortex-M vector table (image base = 0xE000).
- Finds the ARM-linker scatter-load region table and decompresses the
  RW init data (ARM __decompress RLE+LZ variant, see dec()).
- Parses any USB device/config descriptors found in that RW data.
"""
import re, struct, sys

BASE = 0xE000  # app load address (bootloader occupies 0x0000-0xDFFF)

def dec(d, i, size):
    """ARM linker RW compression (__decompress). token: low3=literal count(+1),
    bit3=1 -> back-ref (offset byte, len=hi4+2), bit3=0 -> zero run (len=hi4).
    A zero count field means 'next byte holds the count'."""
    out = bytearray()
    while len(out) < size:
        b = d[i]; i += 1
        lit = b & 7
        if lit == 0: lit = d[i]; i += 1
        m = b >> 4
        if m == 0: m = d[i]; i += 1
        out += d[i:i + lit - 1]; i += lit - 1
        if b & 8:
            p = len(out) - d[i]; i += 1
            for k in range(m + 2): out.append(out[p + k])
        else:
            out += bytes(m)
    return bytes(out[:size])

def find_region_table(d):
    """Look for {src, dst=0x2000xxxx, size, func} 16-byte entries near the end."""
    hits = []
    for o in range(0, len(d) - 16, 4):
        src, dst, size, fn = struct.unpack('<4I', d[o:o + 16])
        if 0x20000000 <= dst < 0x20100000 and BASE <= src < BASE + len(d) + 0x20000 \
           and 0 < size < 0x100000 and BASE <= fn < BASE + len(d):
            hits.append((o, src, dst, size, fn))
    return hits

def parse_usb(buf):
    for m in re.finditer(rb'\x12\x01', buf):
        b = buf[m.start():m.start() + 18]
        if len(b) == 18 and b[7] in (8, 16, 32, 64):
            vid, pid = struct.unpack('<HH', b[8:12])
            print(f'  DEVICE desc @+0x{m.start():x}: bcdUSB={b[3]:x}.{b[2]:02x} class={b[4]} VID=0x{vid:04x} PID=0x{pid:04x} maxpkt0={b[7]}')
    for m in re.finditer(rb'\x09\x02', buf):
        o = m.start(); total = struct.unpack('<H', buf[o + 2:o + 4])[0]
        if not 9 <= total <= 512: continue
        b = buf[o:o + total]; i = 0
        print(f'  CONFIG desc @+0x{o:x} total={total} interfaces={b[4]}')
        while i < len(b) and b[i]:
            L, T = b[i], b[i + 1]
            if T == 4: print(f'    IF {b[i+2]} class=0x{b[i+5]:02x} sub=0x{b[i+6]:02x} proto=0x{b[i+7]:02x} eps={b[i+4]}')
            if T == 5: print(f'    EP 0x{b[i+2]:02x} attr=0x{b[i+3]:02x} maxpkt={struct.unpack("<H", b[i+4:i+6])[0]}')
            i += L
    for s in re.findall(rb'(?:[\x20-\x7e]\x00){3,}', buf):
        print('  UTF16 string:', s.decode('utf-16le'))

def main():
    d = open(sys.argv[1], 'rb').read()
    sp, rst = struct.unpack('<2I', d[:8])
    print(f'size={len(d)} initial SP=0x{sp:08x} Reset=0x{rst:08x} (base 0x{BASE:x})')
    for o, src, dst, size, fn in find_region_table(d):
        print(f'region entry @file+0x{o:x}: src=0x{src:x} dst=0x{dst:x} size=0x{size:x} fn=0x{fn:x}')
        if src - BASE < len(d):
            try:
                rw = dec(d, src - BASE, size)
            except IndexError:
                print('  (not compressed / zero-init region)'); continue
            print(f'  decompressed {len(rw)} bytes; ascii:', re.findall(rb'[\x20-\x7e]{4,}', rw))
            parse_usb(rw)
            if '--dump-rw' in sys.argv:
                out = sys.argv[sys.argv.index('--dump-rw') + 1]
                open(out, 'wb').write(rw); print('  wrote', out)

if __name__ == '__main__':
    main()
