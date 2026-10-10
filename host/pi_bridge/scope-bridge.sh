#!/bin/bash
# Wait for the scope's USB CDC port, then bridge it to TCP 7001 until it goes away.
PORT=${PORT:-7001}
while true; do
    DEV=$(ls /dev/serial/by-id/*OpenScope* /dev/serial/by-id/* /dev/ttyACM0 2>/dev/null | head -1)
    if [ -z "$DEV" ]; then sleep 2; continue; fi
    echo "bridging $DEV -> tcp:$PORT"
    socat TCP-LISTEN:"$PORT",reuseaddr,fork,nodelay "$DEV",raw,echo=0,b115200
    sleep 2
done
