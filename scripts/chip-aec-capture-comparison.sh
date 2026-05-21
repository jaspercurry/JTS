#!/usr/bin/env bash
# Phase 4: ear test. Capture audio files for side-by-side A/B listening.
#
# Outputs land in captures/chip-aec-experiment/<timestamp>/ on the laptop.
# Each capture is ${SECS} seconds (default 30, override via env SECS=N).
#
# What gets captured:
#   01_reference.wav     — what the chip sees as AEC reference (CamillaDSP tap)
#   02_mic_aec_off.wav   — chip ch1 with SHF_BYPASS=1 (chip AEC bypassed)
#   03_mic_aec_on.wav    — chip ch1 with SHF_BYPASS=0 (chip AEC engaged)
#   04_speech_only.wav   — chip ch1 with chip AEC on, no music, user speaks
#
# What to listen for:
#   - 02 vs 03: chip AEC should noticeably reduce music in 03 vs 02.
#     If 03 sounds about the same as 02, chip AEC isn't doing useful work.
#   - 04: chip AEC should NOT degrade your voice. If 04 sounds robotic or
#     suppressed, the chip's residual stage is over-aggressive on speech.
#   - 01: should sound like the music you played. If it's silent or
#     garbled, the reference feeder isn't working.
#
# Pre-conditions:
#   - bash scripts/chip-aec-setup.sh has been run
#   - Convergence already verified (chip-aec-poll-convergence.sh showed
#     AEC_AECCONVERGED = 1)
#
# Interactive — the script prompts you between captures.

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
SECS=${SECS:-30}

TS=$(date +%Y%m%d-%H%M%S)
OUTDIR="captures/chip-aec-experiment/${TS}"
mkdir -p "$OUTDIR"

echo "=== chip-aec-experiment: Phase 4 ear test ==="
echo "Output: ${OUTDIR}/"
echo "Capture length: ${SECS}s each"
echo

prompt() {
  echo
  echo "------------------------------------------"
  echo "$@"
  echo "------------------------------------------"
  read -r -p "Press Enter when ready... " _ < /dev/tty
}

capture_chip_ch() {
  # $1 = label, $2 = output file, $3 = channel index (0-5)
  local label="$1" outfile="$2" ch="$3"
  echo "  capturing ${SECS}s: ${label}"
  local tmp="${OUTDIR}/${outfile}.6ch.tmp"
  ssh "${PI_USER}@${PI_HOST}" \
    "arecord -D hw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 6 -d ${SECS} -t wav 2>/dev/null" > "$tmp"
  python3 - "$tmp" "${OUTDIR}/${outfile}" "$ch" <<'PYEOF'
import sys, wave, numpy as np
src, dst, ch = sys.argv[1], sys.argv[2], int(sys.argv[3])
with wave.open(src, "rb") as w:
    n = w.getnframes()
    pcm = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1, w.getnchannels())
out = pcm[:, ch].copy()
with wave.open(dst, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(out.tobytes())
import os
os.remove(src)
PYEOF
  echo "    wrote ${OUTDIR}/${outfile}"
}

capture_ref() {
  echo "  capturing ${SECS}s: reference (plug:jasper_capture, 16k mono)"
  ssh "${PI_USER}@${PI_HOST}" \
    "arecord -D plug:jasper_capture -f S16_LE -r 16000 -c 1 -d ${SECS} -t wav 2>/dev/null" \
    > "${OUTDIR}/01_reference.wav"
  echo "    wrote ${OUTDIR}/01_reference.wav"
}

set_bypass() {
  ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS $1 >/dev/null"
  echo "  SHF_BYPASS = $1 ($([ "$1" = "0" ] && echo "chip AEC ON" || echo "chip AEC OFF"))"
}

# ---------- Step 1 ----------
prompt "STEP 1/3: Start music playing through the speaker at production volume.
Use any source (AirPlay, Spotify, BT). Wait 30+ s for stable, full-level music.
The script will then run two captures in series: reference (10 s into music),
then the chip's mic ch1 with chip AEC ON."

set_bypass 0
capture_ref
capture_chip_ch "03_mic_aec_on (chip ch1, SHF_BYPASS=0)" "03_mic_aec_on.wav" 1

# ---------- Step 2 ----------
prompt "STEP 2/3: KEEP MUSIC PLAYING.
We'll flip SHF_BYPASS=1 (chip AEC bypassed) and re-capture chip ch1.
This is the apples-to-apples comparison against the previous capture."

set_bypass 1
capture_chip_ch "02_mic_aec_off (chip ch1, SHF_BYPASS=1)" "02_mic_aec_off.wav" 1
set_bypass 0
echo "  restored SHF_BYPASS=0 (chip AEC ON)"

# ---------- Step 3 ----------
prompt "STEP 3/3: STOP music NOW.
Wait for full silence (~10 s).
During the next ${SECS} s, speak a few sentences:
  'Hey Jarvis. Testing one two three. The quick brown fox jumps over the lazy dog.'
This verifies chip AEC doesn't degrade your voice."

capture_chip_ch "04_speech_only (chip ch1, AEC on, no music)" "04_speech_only.wav" 1

# ---------- Summary ----------
echo
echo "=== Summary ==="
python3 - <<PYEOF
import wave, math, os, struct
outdir = "${OUTDIR}"
for fname in sorted(os.listdir(outdir)):
    if not fname.endswith(".wav"):
        continue
    path = os.path.join(outdir, fname)
    try:
        with wave.open(path, "rb") as w:
            n = w.getnframes(); rate = w.getframerate(); nchan = w.getnchannels()
            pcm = w.readframes(n)
        samples = struct.unpack("<" + "h" * (n * nchan), pcm)
        if nchan > 1:
            samples = samples[::nchan]
            n = len(samples)
        mean = sum(samples) / max(n, 1)
        rms = math.sqrt(sum((s - mean) ** 2 for s in samples) / max(n, 1))
        rms_db = 20 * math.log10(max(rms, 1) / 32768)
        peak = max(abs(s) for s in samples) if samples else 0
        peak_db = 20 * math.log10(max(peak, 1) / 32768)
        print(f"  {fname:32s} {n/rate:5.1f}s  RMS={rms:6.0f} ({rms_db:+6.1f} dBFS)  peak={peak:5d} ({peak_db:+6.1f} dBFS)")
    except Exception as e:
        print(f"  {fname}: ERROR {e}")
PYEOF

echo
echo "=== Done ==="
echo "Files: ${OUTDIR}/"
echo
echo "Recommended listening order:"
echo "  1. 01_reference.wav   — should sound like the music you played"
echo "  2. 02_mic_aec_off.wav — music will dominate (chip AEC bypassed)"
echo "  3. 03_mic_aec_on.wav  — music should be substantially reduced (chip AEC engaged)"
echo "  4. 04_speech_only.wav — voice only; chip AEC should NOT degrade speech"
echo
echo "For aligned A/B in sox/Audacity:"
echo "  sox -m ${OUTDIR}/02_mic_aec_off.wav ${OUTDIR}/03_mic_aec_on.wav diff.wav"
echo "  (then listen to diff.wav — silence-ish means they match, residual = AEC effect)"
