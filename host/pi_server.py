#!/usr/bin/env python3
"""Pi-side bridge: scope UART0  <->  TCP (newline-delimited JSON), for remote monitoring/control.

    python3 pi_server.py --port /dev/ttyAMA0 --listen 0.0.0.0:7000

Every client gets a stream of decoded events (one JSON object per line, see fnirsi_uart0.describe)
plus {"type":"raw","hex":...} for every chunk read from the serial port. Clients may send:
    {"cmd": "state"}                          -> {"type":"state", ...last values...}
    {"cmd": "answer", "code": 3, "value": 7}  -> next time the scope queries cmd 3, reply 7
    {"cmd": "tx", "hex": "aa030007..."}       -> write raw bytes (expert use)
Only the stdlib + pyserial are needed.
"""
import argparse, asyncio, json, sys, threading
from fnirsi_uart0 import Engine, open_serial, BAUD

clients = set()
loop = None


def broadcast(obj):
    line = (json.dumps(obj) + '\n').encode()
    for w in list(clients):
        try:
            w.write(line)
        except Exception:
            clients.discard(w)


async def handle(reader, writer):
    clients.add(writer)
    peer = writer.get_extra_info('peername')
    print(f'client {peer} connected', file=sys.stderr)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line)
            except Exception:
                continue
            c = req.get('cmd')
            if c == 'state':
                writer.write((json.dumps({'type': 'state', 'values': {k: v.hex() for k, v in ENGINE.state.items()}}) + '\n').encode())
            elif c == 'answer':
                v = req['value']; n = req.get('nbytes', 1)
                ENGINE.set_answer(int(req['code']), int(v).to_bytes(n, 'big'))
                writer.write(b'{"type":"ok"}\n')
            elif c == 'tx':
                SER.write(bytes.fromhex(req['hex']))
                writer.write(b'{"type":"ok"}\n')
    finally:
        clients.discard(writer); writer.close()
        print(f'client {peer} gone', file=sys.stderr)


def serial_thread():
    while True:
        d = SER.read(64)
        if d:
            loop.call_soon_threadsafe(broadcast, {'type': 'raw', 'hex': d.hex()})
            ENGINE.feed(d)


async def amain(listen):
    global loop
    loop = asyncio.get_running_loop()
    host, port = listen.rsplit(':', 1)
    srv = await asyncio.start_server(handle, host, int(port))
    threading.Thread(target=serial_thread, daemon=True).start()
    print(f'serving on {listen}, serial {SER.port} @ {BAUD}', file=sys.stderr)
    async with srv:
        await srv.serve_forever()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyAMA0'); ap.add_argument('--listen', default='0.0.0.0:7000')
    ap.add_argument('--no-ack', action='store_true')
    a = ap.parse_args()
    SER = open_serial(a.port)
    ENGINE = Engine((lambda b: None) if a.no_ack else SER.write,
                    lambda ev: loop.call_soon_threadsafe(broadcast, dict(ev, type='event')))
    asyncio.run(amain(a.listen))
