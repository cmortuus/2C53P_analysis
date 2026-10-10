#!/usr/bin/env python3
"""Decode a FNIRSI 2C53P "Screenshot simple file/<n>.bin" waveform save.

Verified against real files (2026-10-08, APP V1.14; format unchanged since V1.8):
    0..460    CH1 trace, 461 x u8   (one sample per screen pixel column)
    461,462   CH1 max, min (u8)
    463..923  CH2 trace, 461 x u8
    924,925   CH2 max, min
    926       channel mask: bit0 = CH1 on, bit1 = CH2 on
Total 927 bytes.

Sample value = 8-bit ADC code - 8, clamped to 0..240 (firmware wave_draw_main). The ADC
spans exactly 8 vertical divisions, so 32 px per division; screen row = 268 - value.
Horizontal: 50 px per division, centre (trigger point) at sample 230; the 461 visible
samples cover 9.2 divisions.

NOT stored in the file: timebase, V/div, probe factor and the channel zero positions. Read
them off the matching <n>.bmp status bar / position markers and pass them in:
    --tb      seconds per division
    --vdiv1/2 volts per division as displayed (already includes the X1/X10 probe factor)
    --pos1/2  zero-level position in sample units (0..240; default 120 = screen centre).
              The BMP's channel marker arrow is at screen row y -> pos = 268 - y.

Usage: bin2csv.py FILE.bin [--tb S] [--vdiv1 V] [--vdiv2 V] [--pos1 P] [--pos2 P] [-o out.csv] [--plot]
"""
import argparse, sys, csv

N = 461
PX_PER_DIV_V = 32
PX_PER_DIV_H = 50
CENTER_X = 230

def decode(b):
    if len(b) != 927:
        print(f'warning: unexpected size {len(b)} (expected 927)', file=sys.stderr)
    return dict(ch1=list(b[0:N]), ch1_max=b[461], ch1_min=b[462],
                ch2=list(b[463:463 + N]), ch2_max=b[924], ch2_min=b[925],
                mask=b[926] if len(b) > 926 else 3)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--tb', type=float, help='s/div')
    ap.add_argument('--vdiv1', type=float); ap.add_argument('--vdiv2', type=float)
    ap.add_argument('--pos1', type=float, default=120); ap.add_argument('--pos2', type=float, default=120)
    ap.add_argument('-o', help='csv out (default stdout)'); ap.add_argument('--plot', action='store_true')
    a = ap.parse_args()
    w = decode(open(a.file, 'rb').read())
    on1, on2 = bool(w['mask'] & 1), bool(w['mask'] & 2)
    print(f'mask={w["mask"]} (CH1 {"on" if on1 else "off"}, CH2 {"on" if on2 else "off"})  '
          f'CH1 min/max={w["ch1_min"]}/{w["ch1_max"]}  CH2 min/max={w["ch2_min"]}/{w["ch2_max"]}', file=sys.stderr)
    v1 = lambda p: (p - a.pos1) / PX_PER_DIV_V * a.vdiv1
    v2 = lambda p: (p - a.pos2) / PX_PER_DIV_V * a.vdiv2
    out = open(a.o, 'w', newline='') if a.o else sys.stdout
    wr = csv.writer(out); wr.writerow(['i', 't_s', 'ch1_px', 'ch2_px', 'ch1_V', 'ch2_V'])
    ts = []
    for i in range(N):
        t = (i - CENTER_X) * a.tb / PX_PER_DIV_H if a.tb else i
        ts.append(t)
        wr.writerow([i, t, w['ch1'][i], w['ch2'][i],
                     f'{v1(w["ch1"][i]):.6g}' if a.vdiv1 else '', f'{v2(w["ch2"][i]):.6g}' if a.vdiv2 else ''])
    if a.o: out.close()
    if a.plot:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        if on1: ax.plot(ts, [v1(p) for p in w['ch1']] if a.vdiv1 else w['ch1'], label='CH1', color='c')
        if on2: ax.plot(ts, [v2(p) for p in w['ch2']] if a.vdiv2 else w['ch2'], label='CH2', color='b')
        ax.set_xlabel('t [s]' if a.tb else 'sample'); ax.set_ylabel('V' if (a.vdiv1 or a.vdiv2) else 'px')
        ax.grid(True); ax.legend(); ax.set_title(a.file)
        plt.savefig(a.file.rsplit('.', 1)[0] + '.png', dpi=100); print('plot ->', a.file.rsplit('.', 1)[0] + '.png', file=sys.stderr)

if __name__ == '__main__':
    main()
