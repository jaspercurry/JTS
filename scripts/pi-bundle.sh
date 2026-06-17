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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_diagnostic_redaction.sh"

units=(jasper-camilla jasper-voice shairport-sync)
for u in "${units[@]}"; do
    journalctl -u "$u" --since "$since" --no-pager --output=short-iso \
        2>/dev/null | redact_jasper_diagnostics > "$DIR/${u}.log" || true
done
journalctl --since "$since" --no-pager --output=short-iso \
    -u jasper-camilla -u jasper-voice -u shairport-sync \
    2>/dev/null | redact_jasper_diagnostics > "$DIR/combined.log" || true

# kernel ring buffer (recent) — useful for USB/ALSA issues
dmesg -T --since "$since" 2>/dev/null | redact_jasper_diagnostics \
    > "$DIR/dmesg.log" || \
    dmesg 2>/dev/null | redact_jasper_diagnostics > "$DIR/dmesg.log" || true

# configs (secrets redacted)
{
    for f in \
        /etc/jasper/jasper.env \
        /var/lib/jasper/voice_provider.env \
        /var/lib/jasper-secrets/voice_keys.env \
        /var/lib/jasper-secrets/google_credentials.env \
        /var/lib/jasper/home_assistant.env \
        /var/lib/jasper/transit.env \
        /var/lib/jasper/wifi_guardian.env; do
        [[ -r "$f" ]] || continue
        echo "== $f =="
        cat "$f"
    done
} | redact_jasper_diagnostics > "$DIR/jasper.env.txt" 2>/dev/null || true
cp /etc/camilladsp/v1.yml "$DIR/camilladsp.yml" 2>/dev/null || true
# /etc/asound.conf since PR #223 (2026-05-23); fall back to the
# legacy /root/.asoundrc for older Pis.
cp /etc/asound.conf "$DIR/asoundrc" 2>/dev/null \
    || cp /root/.asoundrc "$DIR/asoundrc" 2>/dev/null \
    || true
cp /etc/modules-load.d/snd-aloop.conf "$DIR/snd-aloop.conf" 2>/dev/null || true

# unit files for cross-version diff. Redact in case an operator hotfix
# or older install used inline Environment=... secrets instead of
# EnvironmentFile=.
for unit in jasper-camilla.service jasper-voice.service; do
    src="/etc/systemd/system/${unit}"
    [[ -r "$src" ]] || continue
    redact_jasper_diagnostics < "$src" > "$DIR/${unit}" 2>/dev/null || true
done

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
    2>&1 | redact_jasper_diagnostics > "$DIR/systemctl-status.txt" || true

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
