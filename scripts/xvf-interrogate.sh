#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Comprehensive XVF3800 chip + Pi-side diagnostic capture for
# cross-Pi or cross-chip diff workflows.
#
# Always tags output by chip iSerial so we never confuse chip
# identity from physical position (the lesson of the 2026-05-15
# jts2 raw-mic-silent debug).
#
# Usage:
#   bash scripts/xvf-interrogate.sh --host jts.local
#   bash scripts/xvf-interrogate.sh --host jts2.local --label chipA-port1
#
# Output: logs/xvf-interrogate-<serial>-<host>[-<label>]-<utc>.txt
#
# What it captures (covers every hypothesis lane in
# docs/HANDOFF-xvf3800.md §7):
#   1. USB identity + iSerial (lsusb)
#   2. USB topology + negotiated speed (lsusb -t) — hypothesis 7.1
#   3. Full USB descriptor (lsusb -v)
#   4. ALSA enumeration (arecord -l, /proc/asound/Array/stream*)
#      — /proc/asound/Array/stream0 channel count is hypothesis 7.6
#   5. dmesg subset (xhci/usb-audio/snd-usb/underrun)
#   6. Pi hardware (revision, RAM bits, throttled, bootloader)
#   7. snd-usb-audio module parameters
#   8. Full XVF parameter sweep (~30 params)
#   9. 6-channel audio capture, per-channel RMS+peak, on BOTH
#      plughw and hw paths
#  10. XVF firmware artifacts found on disk
#
# Idempotent. Pauses jasper-aec-bridge for the duration; restores
# state on exit. No deploy required — pure SSH.

set -euo pipefail

HOST=""
LABEL=""

usage() {
    # Drop the SPDX license header (reuse inserts it at lines 2-6) so the
    # extracted doc block is the original prose, not the license text.
    sed '2,6d' "$0" | sed -n '2,30p'
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --host)  HOST="$2";  shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "Usage: $0 --host <hostname> [--label <text>]" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS_DIR="$REPO_ROOT/logs"
mkdir -p "$LOGS_DIR"

echo "→ Collecting XVF interrogation from $HOST"
echo "  (~60-90 s — param sweep + 5 s × 2 audio captures)"
echo

# The entire diagnostic runs as one heredoc on the Pi, as root.
# Output is captured here and parsed for iSerial → filename.
REMOTE_OUTPUT="$(ssh "pi@$HOST" 'sudo bash -s' <<'REMOTE'
set +e  # diagnostics: keep going on individual failures

PY=/opt/jasper/.venv/bin/python
M="-m jasper.xvf.xvf_host"

echo "===== XVF-INTERROGATE v1 ====="
echo "pi_hostname=$(hostname)"
echo "pi_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

# --- 0. Service state preflight ---
echo "----- 0. Service state -----"
BRIDGE_WAS_ACTIVE=false
VOICE_WAS_ACTIVE=false
if systemctl is-active --quiet jasper-aec-bridge; then BRIDGE_WAS_ACTIVE=true; fi
if systemctl is-active --quiet jasper-voice;      then VOICE_WAS_ACTIVE=true;  fi
echo "bridge_was_active=$BRIDGE_WAS_ACTIVE"
echo "voice_was_active=$VOICE_WAS_ACTIVE"

for unit in jasper-aec-bridge jasper-aec-init jasper-voice jasper-camilla; do
    enabled=$(systemctl is-enabled "$unit" 2>&1 || true)
    active=$(systemctl is-active "$unit" 2>&1 || true)
    echo "service: $unit  enabled=$enabled  active=$active"
done

# Both daemons can hold the mic open — stop whichever is active so
# arecord can read it for the per-channel RMS section.
if [ "$BRIDGE_WAS_ACTIVE" = "true" ]; then
    echo "stopping jasper-aec-bridge to free the chip"
    systemctl stop jasper-aec-bridge
fi
if [ "$VOICE_WAS_ACTIVE" = "true" ]; then
    echo "stopping jasper-voice to free the chip"
    systemctl stop jasper-voice
fi
sleep 1

# Restore on exit
restore_state() {
    if [ "$VOICE_WAS_ACTIVE" = "true" ]; then
        echo
        echo "----- exit: restoring jasper-voice -----"
        systemctl start jasper-voice 2>&1 || true
    fi
    if [ "$BRIDGE_WAS_ACTIVE" = "true" ]; then
        echo "----- exit: restoring jasper-aec-bridge -----"
        systemctl start jasper-aec-bridge 2>&1 || true
    fi
}
trap restore_state EXIT

echo

# --- 1. USB identity + iSerial ---
echo "----- 1. USB identity -----"
lsusb -d 2886:001a 2>&1 | head -5

SERIAL=$(lsusb -v -d 2886:001a 2>/dev/null | grep -m1 iSerial | awk '{print $3}')
[ -z "$SERIAL" ] && SERIAL=unknown
echo "chip_iserial=$SERIAL"
echo

# --- 1b. USB descriptor head ---
echo "----- 1b. USB descriptor (first 100 lines) -----"
lsusb -v -d 2886:001a 2>/dev/null | head -100
echo

# --- 1c. USB endpoint sizes (audio interface) ---
echo "----- 1c. USB audio endpoint maxPacketSize (key for hypothesis 7.1) -----"
lsusb -v -d 2886:001a 2>/dev/null \
    | grep -E 'Interface Descriptor|bInterfaceClass|bAlternateSetting|wMaxPacketSize|bInterval|bNrChannels|tSamFreq|bSubframeSize' \
    | head -60
echo

# --- 2. USB topology + negotiated speed ---
echo "----- 2. USB topology (lsusb -t) -----"
lsusb -t 2>&1
echo
echo "----- 2b. Class=Audio rows (XVF should be in here at 480M) -----"
# lsusb -t does not print VID:PID, so we cross-reference: which bus
# does the XVF live on (per regular lsusb), and which Class=Audio
# row sits on that bus in the tree?
XVF_BUS_DEV=$(lsusb -d 2886:001a 2>/dev/null | head -1 \
              | awk '{ gsub(":","",$4); printf "bus=%s dev=%s", $2, $4 }')
echo "xvf_bus_dev: $XVF_BUS_DEV"
lsusb -t 2>&1 | grep -B3 -A0 'Class=Audio' || true
echo

# --- 3. ALSA card enumeration ---
echo "----- 3. arecord -l (capture devices) -----"
arecord -l 2>&1 | head -40
echo
echo "----- 3b. /proc/asound/cards -----"
cat /proc/asound/cards 2>&1 | head -20
echo
echo "----- 3c. /proc/asound/Array/stream0 (KEY — hypothesis 7.6) -----"
cat /proc/asound/Array/stream0 2>&1
echo
echo "----- 3d. /proc/asound/Array/stream1 (playback side) -----"
cat /proc/asound/Array/stream1 2>&1 || echo "(no stream1)"
echo
echo "----- 3e. amixer Array contents (top 80) -----"
amixer -c Array contents 2>&1 | head -80
echo

# --- 4. dmesg subset ---
echo "----- 4. dmesg — USB/audio/xhci (tail 60) -----"
dmesg -T 2>&1 \
    | grep -iE '2886:001a|reSpeaker|XVF3800|usb-audio|snd-usb|underrun|xhci|dwc|hub' \
    | tail -60
echo

# --- 5. Pi hardware ---
echo "----- 5. Pi hardware -----"
echo "uname=$(uname -a)"
echo "kernel=$(uname -r)"
grep -iE '^model|^revision|^serial|^hardware' /proc/cpuinfo 2>&1 | tail -8
echo
grep -iE 'memtotal|memfree|memavail' /proc/meminfo 2>&1 | head -3
echo
echo "throttled=$(vcgencmd get_throttled 2>&1)"
echo "vcgencmd_version:"
vcgencmd version 2>&1 | head -5
echo "bootloader:"
vcgencmd bootloader_version 2>&1 | head -5
rpi-eeprom-update 2>&1 | head -10
echo

# Decode revision code into RAM size + processor SKU
/usr/bin/python3 <<'PYEOF'
try:
    with open('/proc/cpuinfo') as f:
        for line in f:
            if line.startswith('Revision'):
                rev_hex = line.split(':', 1)[1].strip()
                break
        else:
            print("revcode=not-found")
            raise SystemExit(0)
    rev = int(rev_hex, 16)
    new_style = (rev >> 23) & 1
    if new_style:
        ram_bits = (rev >> 20) & 7
        ram = {0:'256M', 1:'512M', 2:'1G', 3:'2G', 4:'4G', 5:'8G'}.get(ram_bits, f'unknown({ram_bits})')
        proc_bits = (rev >> 12) & 0xF
        proc = {0:'BCM2835', 1:'BCM2836', 2:'BCM2837', 3:'BCM2711', 4:'BCM2712'}.get(proc_bits, f'unknown({proc_bits})')
        type_bits = (rev >> 4) & 0xFF
        type_map = {0x11:'CM4', 0x12:'Zero2W', 0x13:'Pi400', 0x14:'CM4S', 0x17:'Pi5', 0x18:'CM5'}
        ptype = type_map.get(type_bits, f'unknown(0x{type_bits:02x})')
        print(f"revcode={rev_hex} model={ptype} ram={ram} proc={proc}")
    else:
        print(f"revcode={rev_hex} (old-style, not decoded)")
except Exception as e:
    print(f"revcode_decode_error={e}")
PYEOF
echo
echo "----- 5b. /proc/cmdline -----"
cat /proc/cmdline 2>&1
echo

# --- 6. snd-usb-audio module parameters ---
echo "----- 6. snd-usb-audio module parameters -----"
if [ -d /sys/module/snd_usb_audio/parameters/ ]; then
    for p in /sys/module/snd_usb_audio/parameters/*; do
        echo "snd_usb_audio.$(basename "$p")=$(cat "$p" 2>&1)"
    done
else
    echo "(snd_usb_audio module not loaded)"
fi
echo

# --- 7. XVF chip parameter sweep ---
echo "----- 7. XVF parameter sweep -----"
echo "Note: each line prefixed with 'param:' for easy grep+diff"

PARAMS="VERSION BLD_MSG BLD_HOST BLD_REPO_HASH BLD_MODIFIED BOOT_STATUS"
PARAMS="$PARAMS USB_BIT_DEPTH"
PARAMS="$PARAMS AEC_NUM_MICS AEC_NUM_FARENDS AEC_MIC_ARRAY_TYPE"
PARAMS="$PARAMS AEC_AECCONVERGED AEC_HPFONOFF"
PARAMS="$PARAMS SHF_BYPASS"
PARAMS="$PARAMS AUDIO_MGR_MIC_GAIN AUDIO_MGR_REF_GAIN"
PARAMS="$PARAMS AUDIO_MGR_SELECTED_CHANNELS"
PARAMS="$PARAMS AUDIO_MGR_OP_PACKED AUDIO_MGR_OP_UPSAMPLE"
PARAMS="$PARAMS AUDIO_MGR_OP_L AUDIO_MGR_OP_R AUDIO_MGR_OP_ALL"
PARAMS="$PARAMS AUDIO_MGR_FAR_END_DSP_ENABLE"
PARAMS="$PARAMS AUDIO_MGR_SYS_DELAY"
PARAMS="$PARAMS I2S_INACTIVE I2S_DAC_DSP_ENABLE"
PARAMS="$PARAMS GPO_READ_VALUES"
PARAMS="$PARAMS LED_EFFECT LED_BRIGHTNESS"

for p in $PARAMS; do
    val=$(timeout 5 $PY $M "$p" 2>&1 \
          | grep -v 'Done!' \
          | tr '\n' ' ' \
          | sed 's/  */ /g; s/^ //; s/ $//')
    echo "param: $p = $val"
done
echo

# --- 8. Per-channel audio activity check ---
echo "----- 8. 6-channel audio capture (5 s @ 16 kHz S16_LE) -----"
echo "Identical capture on BOTH plughw and hw paths to expose any"
echo "ALSA-conversion-layer divergence (rare but possible)."

probe_channels() {
    local label="$1"
    local device="$2"
    echo
    echo ">>> path: $label  device: $device"

    local raw=/tmp/xvf6-$$.raw
    timeout 10 arecord -D "$device" -c 6 -r 16000 -f S16_LE -d 5 -q -t raw \
        > "$raw" 2>/tmp/xvf-arec-err-$$
    local rc=$?
    local bytes
    bytes=$(stat -c%s "$raw" 2>/dev/null || echo 0)

    if [ "$rc" -ne 0 ] || [ "$bytes" -eq 0 ]; then
        echo "CAPTURE FAILED (rc=$rc, bytes=$bytes)"
        echo "arecord stderr:"
        cat /tmp/xvf-arec-err-$$ 2>/dev/null | head -10
        rm -f "$raw" /tmp/xvf-arec-err-$$
        return
    fi

    /usr/bin/python3 - "$raw" <<'PYEOF'
import struct, sys, math
path = sys.argv[1]
with open(path, 'rb') as f:
    data = f.read()
samples = struct.unpack(f'<{len(data)//2}h', data) if data else []
nch = 6
n_per = len(samples) // nch
print(f"bytes={len(data)}  samples_per_channel={n_per}  duration_ms={n_per*1000//16000}")
for ch in range(nch):
    s = samples[ch::nch]
    if not s:
        print(f"ch{ch}: NO_SAMPLES")
        continue
    rms = math.sqrt(sum(x*x for x in s) / len(s))
    peak = max(abs(x) for x in s)
    rms_db = 20 * math.log10(rms / 32768) if rms > 0 else float('-inf')
    if peak == 0:
        tag = "DEAD"
    elif peak <= 4:
        tag = "near-zero"
    elif peak <= 100:
        tag = "very-quiet"
    else:
        tag = "ACTIVE"
    rms_db_str = f"{rms_db:7.2f}" if rms > 0 else "  -inf "
    print(f"ch{ch}: rms={rms:8.1f}  rms_dB={rms_db_str}  peak={peak:6d}  [{tag}]")
PYEOF
    rm -f "$raw" /tmp/xvf-arec-err-$$
}

probe_channels "plughw"  "plughw:CARD=Array,DEV=0"
probe_channels "hw"      "hw:CARD=Array,DEV=0"
echo

# --- 9. XVF firmware artifacts on disk ---
echo "----- 9. XVF firmware binaries on disk -----"
find /home/pi /opt/jasper /usr/local -name 'respeaker_xvf3800_*' 2>/dev/null | head -10
echo "MD5 of any 6-ch firmware found:"
find /home/pi /opt/jasper /usr/local -name 'respeaker_xvf3800_*6chl*' 2>/dev/null \
    | xargs -r md5sum 2>/dev/null
echo

# --- 10. Mic env that voice would use ---
echo "----- 10. Voice mic env -----"
grep -E '^JASPER_MIC|^JASPER_AEC' /etc/jasper/jasper.env 2>/dev/null | head -10
echo "aec_mode.env:"
cat /var/lib/jasper/aec_mode.env 2>&1 | head -5
echo

echo "===== XVF-INTERROGATE END ====="
REMOTE
)"

# Parse out the iSerial for the output filename
SERIAL=$(echo "$REMOTE_OUTPUT" | grep -m1 '^chip_iserial=' | cut -d= -f2 | tr -d ' \r')
[ -z "$SERIAL" ] && SERIAL=unknown

UTC=$(date -u +%Y%m%dT%H%M%SZ)
HOST_SHORT=${HOST%.local}
LABEL_SUFFIX=""
[ -n "$LABEL" ] && LABEL_SUFFIX="-$LABEL"

OUT="$LOGS_DIR/xvf-interrogate-${SERIAL}-${HOST_SHORT}${LABEL_SUFFIX}-${UTC}.txt"

{
    echo "# xvf-interrogate v1"
    echo "# laptop_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# host: $HOST"
    echo "# chip_iserial: $SERIAL"
    echo "# label: ${LABEL:-(none)}"
    echo "# repo_sha: $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "# script: scripts/xvf-interrogate.sh"
    echo
    echo "$REMOTE_OUTPUT"
} > "$OUT"

echo
echo "→ Wrote: $OUT"
echo
echo "Headline summary:"
echo "  chip iSerial: $SERIAL"
{
    echo -n "  USB speed: "
    grep -m1 'Bus.*001a' "$OUT" >/dev/null 2>&1 && sed -n '/----- 2b/,/^----- 3/p' "$OUT" | grep -m1 -oE '[0-9]+M' || true
    echo -n "  ALSA stream0 capture channels: "
    # stream0 has Playback then Capture, each with their own Channels:
    # line; we want the Capture one. Pin to the Capture: section.
    sed -n '/----- 3c/,/----- 3d/p' "$OUT" \
        | awk '/^Capture:/{cap=1} cap && /Channels:/{print; exit}' \
        | tr -s ' '
    echo "  Per-channel activity (plughw):"
    sed -n '/path: plughw/,/path: hw/p' "$OUT" | grep -E '^ch[0-5]:'
    echo "  Per-channel activity (hw):"
    sed -n '/path: hw/,/----- 9/p' "$OUT" | grep -E '^ch[0-5]:'
} 2>&1
echo
echo "Compare against another cell:"
echo "  diff $OUT $LOGS_DIR/xvf-interrogate-<other>.txt"
