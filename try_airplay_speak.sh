#!/bin/bash
# Standalone check that RAOP playback reaches the HomePod, with no BlinkServer,
# no Flask and no Home Assistant. Run this on a Mac on the speaker's LAN.
#
#   ./try_airplay_speak.sh                                   # defaults
#   ./try_airplay_speak.sh "Dinner is ready"
#   ./try_airplay_speak.sh "おかえりなさい" 10.0.0.155 Kyoko
#
# The recording goes to audio/ and is KEPT, using the same directory and naming
# as jobs/airplay_speak.py — so you can play back what was said, and
# POST /webhook/speak/purge trims these alongside the server's own recordings.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-/tmp/blinkvenv/bin/python}"
TEXT="${1:-Hello from Blink Server}"
HOST="${2:-10.0.0.155}"
VOICE="${3:-}"

AUDIO_DIR="$HERE/audio"
mkdir -p "$AUDIO_DIR"
# Matches the job's pattern — speak_<date>_<time>_<millis>_<who>.wav — so the two
# sort together chronologically and purge's speak_*.wav glob picks these up too.
AUDIO="$AUDIO_DIR/speak_$(date +%Y%m%d_%H%M%S)_000_script.wav"

echo "1. Rendering \"$TEXT\" with macOS say${VOICE:+ (voice: $VOICE)}"
if [ -n "$VOICE" ]; then
    say -v "$VOICE" -o "$AUDIO" --data-format=LEI16@44100 -- "$TEXT"
else
    say -o "$AUDIO" --data-format=LEI16@44100 -- "$TEXT"
fi
ls -lh "$AUDIO"

# A voice that cannot speak the text produces a near-empty file while `say` still
# exits 0 — Japanese through an English voice gives ~50ms. Catch that here rather
# than wondering why the HomePod was silent.
BYTES=$(stat -f%z "$AUDIO")
if [ "$BYTES" -lt 8000 ]; then
    echo "   WARNING: only $BYTES bytes — under a tenth of a second."
    echo "   Is the voice right for this text? Japanese needs a voice like Kyoko."
fi

echo "2. Streaming it to $HOST over RAOP"
"$PY" - "$HOST" "$AUDIO" <<'PYEOF'
import asyncio, sys
import pyatv

async def main(host, audio):
    loop = asyncio.get_running_loop()
    found = await pyatv.scan(loop, hosts=[host], timeout=5)
    if not found:
        raise SystemExit(f"nothing answered at {host}")
    print("   found:", found[0].name, "-", [s.protocol.name for s in found[0].services])
    atv = await pyatv.connect(found[0], loop)
    try:
        await atv.stream.stream_file(audio)
        print("   stream_file completed")
    finally:
        atv.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
PYEOF

echo "3. Done — the HomePod should have spoken."
echo "   Kept at:  $AUDIO"
echo "   Play it:  afplay \"$AUDIO\""
