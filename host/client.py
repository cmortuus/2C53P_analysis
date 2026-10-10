#!/usr/bin/env python3
"""Mac-side client for pi_server.py.

    python3 client.py --pi scope-pi.local:7000            # live decoded stream
    python3 client.py --pi scope-pi.local:7000 --state     # dump last-known values
    python3 client.py --pi scope-pi.local:7000 --answer 3=7   # when scope next queries cmd 3, reply 7
    python3 client.py --pi scope-pi.local:7000 --log uart0.jsonl
"""
import argparse, json, socket, sys
from fnirsi_uart0 import COMMANDS

ap = argparse.ArgumentParser()
ap.add_argument('--pi', default='scope-pi.local:7000')
ap.add_argument('--state', action='store_true'); ap.add_argument('--answer', action='append', default=[])
ap.add_argument('--raw', action='store_true', help='also print raw serial chunks')
ap.add_argument('--log')
a = ap.parse_args()
host, port = a.pi.rsplit(':', 1)
s = socket.create_connection((host, int(port)))
f = s.makefile('rw')
for spec in a.answer:
    code, val = spec.split('='); f.write(json.dumps({'cmd': 'answer', 'code': int(code, 0), 'value': int(val, 0)}) + '\n')
if a.state:
    f.write('{"cmd":"state"}\n')
f.flush()
log = open(a.log, 'a') if a.log else None
for line in f:
    ev = json.loads(line)
    if log: log.write(line); log.flush()
    t = ev.get('type')
    if t == 'event':
        print(f"{ev['hdr']:6s} cmd=0x{ev['cmd']:02x} {ev['name']:18s} value={ev['value']}  {ev.get('action','')}  [{ev['raw']}]")
    elif t == 'state':
        for k, v in sorted(ev['values'].items(), key=lambda kv: int(kv[0])):
            print(f"  0x{int(k):02x} {COMMANDS.get(int(k), ('?',))[0]:18s} = {v}")
        if not a.answer and not log: break
    elif t == 'raw' and a.raw:
        print('raw', ev['hex'])
    elif t == 'ok':
        pass
