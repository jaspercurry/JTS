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

daemon_set_mode() {
  # Switch the experiment daemon between full mode (ref feeder + UDP
  # mic pump) and --ref-only mode (just ref feeder).
  #
  # Why we need this: capture_chip_ch() opens hw:CARD=Array,DEV=0 in
  # CAPTURE mode via arecord. UAC2 capture is a single-substream
  # endpoint — concurrent opens fail with -EBUSY. The production AEC
  # bridge documents this at jasper/cli/aec_bridge.py:492-495 ("the
  # bridge already holds the Array card exclusively"). During Phase
  # 4 we run the daemon in --ref-only so its udp_mic_pump thread
  # releases the chip's capture side, letting arecord open it. The
  # ref feeder keeps running so the chip's AEC adaptive filter
  # doesn't lose convergence between captures.
  #
  # Side effect: jasper-voice gets no UDP frames during --ref-only,
  # so wake detection is off. Phase 4 doesn't need wake — the user
  # is producing audio for arecord to capture, not interacting with
  # the assistant. The user's "Hey Jarvis. Testing one two three..."
  # in step 3 is a test phrase for the recording, not a wake trigger.
  local mode="$1"
  local extra=""
  if [ "$mode" = "ref-only" ]; then extra="--ref-only"; fi
  ssh "${PI_USER}@${PI_HOST}" "sudo pkill -f 'jasper.chip_aec_experiment' 2>/dev/null || true
sleep 0.5
sudo bash -c \"nohup /opt/jasper/.venv/bin/python -m jasper.chip_aec_experiment ${extra} >> /var/log/chip-aec-experiment.log 2>&1 < /dev/null &\"
sleep 2
if ! pgrep -f 'jasper.chip_aec_experiment' > /dev/null; then
  echo '    FAILED to restart daemon in ${mode} mode — last 10 log lines:'
  sudo tail -10 /var/log/chip-aec-experiment.log
  exit 1
fi
echo '    daemon now in ${mode} mode (PID '\$(pgrep -f jasper.chip_aec_experiment)')'"
}

capture_chip_ch() {
  # $1 = label, $2 = output file, $3 = channel index (0-5)
  # Pre-condition: daemon is in --ref-only mode (mic-pump released
  # the chip's capture side). See daemon_set_mode() above.
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
then the chip's mic ch1 with chip AEC ON.

NOTE: switching daemon to --ref-only for the chip-mic captures (daemon's
mic-pump would otherwise hold hw:CARD=Array,DEV=0 exclusively → arecord
EBUSY). Wake detection in jasper-voice is suspended for the duration of
Phase 4 as a side effect — this is fine; we're recording, not waking."

echo
echo "==> Switching daemon to --ref-only (releases chip capture for arecord)"
daemon_set_mode ref-only

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

# ---------- Restore daemon to full mode ----------
echo
echo "==> Switching daemon back to full mode (resumes UDP mic pump)"
daemon_set_mode full

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
