#!/usr/bin/env bash
# Run ON THE PI. Bundles logs + configs + ALSA state into a single
# tarball at /tmp/jasper-bundle-<TS>.tar.gz, ready to scp off-device.
#
# Usage on the Pi:
#   sudo bash /home/pi/jts/scripts/pi-bundle.sh
#
# Then from your laptop:
#   scp pi@jts.local:/tmp/jasper-bundle-*.tar.gz ./logs/
set -euo pipefail

TS=$(date -u +%Y%m%dT%H%M%SZ)
DIR=/tmp/jasper-bundle-${TS}
mkdir -p "$DIR"

since="${SINCE:-2 hours ago}"

units=(jasper-camilla jasper-voice shairport-sync)
for u in "${units[@]}"; do
    journalctl -u "$u" --since "$since" --no-pager --output=short-iso \
        > "$DIR/${u}.log" 2>/dev/null || true
done
journalctl --since "$since" --no-pager --output=short-iso \
    -u jasper-camilla -u jasper-voice -u shairport-sync \
    > "$DIR/combined.log" 2>/dev/null || true

# kernel ring buffer (recent) — useful for USB/ALSA gremlins
dmesg -T --since "$since" > "$DIR/dmesg.log" 2>/dev/null || \
    dmesg > "$DIR/dmesg.log" 2>/dev/null || true

# configs (secrets redacted)
sed -E 's/^(GEMINI_API_KEY|SPOTIFY_CLIENT_SECRET)=.*/\1=<redacted>/' \
    /etc/jasper/jasper.env > "$DIR/jasper.env.txt" 2>/dev/null || true
cp /etc/camilladsp/v1.yml "$DIR/camilladsp.yml" 2>/dev/null || true
# /etc/asound.conf since PR #223 (2026-05-23); fall back to the
# legacy /root/.asoundrc for older Pis.
cp /etc/asound.conf "$DIR/asoundrc" 2>/dev/null \
    || cp /root/.asoundrc "$DIR/asoundrc" 2>/dev/null \
    || true
cp /etc/modules-load.d/snd-aloop.conf "$DIR/snd-aloop.conf" 2>/dev/null || true

# unit files for cross-version diff
cp /etc/systemd/system/jasper-camilla.service "$DIR/" 2>/dev/null || true
cp /etc/systemd/system/jasper-voice.service "$DIR/" 2>/dev/null || true

# ALSA discovery
{
    echo "== aplay -L =="; aplay -L 2>&1
    echo; echo "== arecord -L =="; arecord -L 2>&1
    echo; echo "== aplay -l =="; aplay -l 2>&1
    echo; echo "== arecord -l =="; arecord -l 2>&1
} > "$DIR/alsa-devices.txt"

# unit status
systemctl status --no-pager \
    jasper-camilla jasper-voice shairport-sync \
    > "$DIR/systemctl-status.txt" 2>&1 || true

# voice sessions / spend
sqlite3 /var/lib/jasper/usage.db \
    'SELECT id, started_at, ended_at, input_tokens, output_tokens, cost_usd FROM sessions ORDER BY id DESC LIMIT 50' \
    > "$DIR/recent-sessions.txt" 2>/dev/null || true

# software versions
{
    echo "== uname =="; uname -a
    echo; echo "== os-release =="; cat /etc/os-release
    echo; echo "== camilladsp =="; /opt/camilladsp/camilladsp --version 2>/dev/null
    echo; echo "== python pkgs =="; /opt/jasper/.venv/bin/pip freeze 2>/dev/null | grep -E '^(google-genai|pycamilladsp|openwakeword|sounddevice|spotipy|httpx|onnxruntime|numpy)='
    echo; echo "== meminfo =="; head -3 /proc/meminfo
} > "$DIR/versions.txt" 2>/dev/null || true

# tar it up
TARBALL="/tmp/jasper-bundle-${TS}.tar.gz"
tar czf "$TARBALL" -C /tmp "$(basename "$DIR")"
rm -rf "$DIR"

echo "$TARBALL"
