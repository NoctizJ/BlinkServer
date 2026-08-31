#!/bin/bash
# Standalone check that RAOP playback reaches the HomePod, with no BlinkServer,
# no Flask and no Home Assistant. Run this on a Mac on the speaker's LAN.
set -e
PY="${PY:-/tmp/blinkvenv/bin/python}"
TEXT="${1:-Hello from Blink Server}"
HOST="${2:-10.0.0.155}"

echo "1. Rendering \"$TEXT\" with macOS say"
say -o /tmp/airplay_test.wav --data-format=LEI16@44100 -- "$TEXT"
ls -l /tmp/airplay_test.wav

echo "2. Streaming it to $HOST over RAOP"
"$PY" - "$HOST" <<'PYEOF'
import asyncio, sys
import pyatv

async def main(host):
    loop = asyncio.get_running_loop()
    found = await pyatv.scan(loop, hosts=[host], timeout=5)
    if not found:
        raise SystemExit(f"nothing answered at {host}")
    print("   found:", found[0].name, "-", [s.protocol.name for s in found[0].services])
    atv = await pyatv.connect(found[0], loop)
    try:
        await atv.stream.stream_file("/tmp/airplay_test.wav")
        print("   stream_file completed")
    finally:
        atv.close()

asyncio.run(main(sys.argv[1]))
PYEOF
rm -f /tmp/airplay_test.wav
echo "3. Done - the HomePod should have spoken."
