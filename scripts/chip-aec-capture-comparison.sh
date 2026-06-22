#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Phase 4: ear test. Capture audio files for side-by-side A/B listening.
#
# Outputs land in captures/chip-aec-experiment/<timestamp>/ on the laptop.
# Each capture is ${SECS} seconds (default 30, override via env SECS=N).
#
# What gets captured:
#   01_reference.wav     — what the chip sees as AEC reference (CamillaDSP tap)
#   02_mic_aec_off.wav   — selected chip channel with SHF_BYPASS=1
#   03_mic_aec_on.wav    — selected chip channel with SHF_BYPASS=0
#   04_speech_only.wav   — selected chip channel, chip AEC on, no music
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
REF_DELAY_MS="${REF_DELAY_MS:-${JASPER_CHIP_AEC_REF_DELAY_MS:-0}}"
MIC_CHANNEL="${MIC_CHANNEL:-${JASPER_CHIP_AEC_MIC_CHANNEL:-0}}"

TS=$(date +%Y%m%d-%H%M%S)
OUTDIR="captures/chip-aec-experiment/${TS}"
mkdir -p "$OUTDIR"

echo "=== chip-aec-experiment: Phase 4 ear test ==="
echo "Output: ${OUTDIR}/"
echo "Capture length: ${SECS}s each"
echo "Reference delay: ${REF_DELAY_MS} ms"
echo "Mic channel: ${MIC_CHANNEL}"
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
  #
  # Race avoidance: we wait for the OLD daemon to fully exit before
  # starting the new one. Without this wait, the new daemon may try
  # to open the chip's USB-IN PCM while the old one is still
  # releasing it, getting -EBUSY. We also verify the new daemon
  # opened its PCMs successfully by grepping for the "open failed"
  # error pattern the daemon logs in that case — pgrep alone proves
  # the process started, not that its threads are actually pumping.
  local mode="$1"
  local extra=""
  if [ "$mode" = "ref-only" ]; then extra="--ref-only"; fi
  ssh "${PI_USER}@${PI_HOST}" "set -e
    module='jasper.chip_aec'_experiment
    module_re='[j]asper[.]chip_aec_experiment'
    # Mark the log boundary so we can scope grep to lines from the
    # NEW daemon only (older 'open failed' lines from a previous
    # run shouldn't false-positive us).
    boundary=\$(wc -l < /var/log/chip-aec-experiment.log 2>/dev/null || echo 0)

    sudo pkill -f \"\$module_re\" 2>/dev/null || true

    # Wait up to 4 s for the daemon to actually exit (10 × 0.4 s).
    # SIGTERM → daemon's main loop wakes from sleep 0.5, joins
    # threads with 3 s timeout. In practice exit happens within 1 s,
    # but ALSA close can stall briefly if the USB endpoint is wedged.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f \"\$module_re\" > /dev/null || break
      sleep 0.4
    done
    if pgrep -f \"\$module_re\" > /dev/null; then
      echo '    old daemon did not exit after SIGTERM; sending SIGKILL'
      sudo pkill -9 -f \"\$module_re\" || true
      sleep 0.5
    fi

    sudo bash -c \"nohup /opt/jasper/.venv/bin/python -m \$module --ref-delay-ms '${REF_DELAY_MS}' --mic-channel '${MIC_CHANNEL}' ${extra} >> /var/log/chip-aec-experiment.log 2>&1 < /dev/null &\"

    # Give the new daemon 2 s to either get to its first log line or
    # crash trying to open a PCM. 2 s is well past the chip's USB
    # enumeration recovery window.
    sleep 2

    if ! pgrep -f \"\$module_re\" > /dev/null; then
      echo '    FAILED to restart daemon in ${mode} mode (process exited) — last 20 log lines:'
      sudo tail -20 /var/log/chip-aec-experiment.log
      exit 1
    fi

    # Process is alive — but did its PCMs open successfully? Scan
    # lines APPENDED to the log since 'boundary' for the daemon's
    # 'open failed' phrase (emitted by reference_feeder / udp_mic_pump
    # on alsaaudio.ALSAAudioError at PCM construction).
    new_lines=\$(sudo tail -n +\$((boundary + 1)) /var/log/chip-aec-experiment.log 2>/dev/null || true)
    if echo \"\$new_lines\" | grep -qE '(ref feeder|mic pump) open failed'; then
      echo '    daemon process alive but PCM open failed — log excerpt:'
      echo \"\$new_lines\" | grep -E 'open failed' | head -5
      exit 1
    fi

    echo \"    daemon now in ${mode} mode (PID \$(pgrep -f \"\$module_re\"))\"
  "
}

capture_chip_ch() {
  # $1 = label, $2 = output file, $3 = channel index (0-5)
  # Pre-condition: daemon is in --ref-only mode (mic-pump released
  # the chip's capture side). See daemon_set_mode() above.
  local label="$1" outfile="$2" ch="$3"
  echo "  capturing ${SECS}s: ${label}"
  local tmp="${OUTDIR}/${outfile}.6ch.tmp"
  local stderr_file="${OUTDIR}/${outfile}.arecord.stderr"
  # Capture stderr separately so we can surface arecord's actual
  # error (EBUSY, device gone, etc.) instead of silently writing a
  # 0-byte WAV that crashes the channel-extract python below.
  if ! ssh "${PI_USER}@${PI_HOST}" \
       "arecord -D hw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 6 -d ${SECS} -t wav" \
       > "$tmp" 2> "$stderr_file"; then
    echo "  arecord exited non-zero. stderr:"
    sed 's/^/    /' "$stderr_file"
    exit 1
  fi
  if [[ ! -s "$tmp" ]]; then
    echo "  arecord produced 0-byte file (chip CAPTURE may still be held by the daemon, or device gone). stderr:"
    sed 's/^/    /' "$stderr_file"
    exit 1
  fi
  rm -f "$stderr_file"
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
  local out="${OUTDIR}/01_reference.wav"
  local stderr_file="${OUTDIR}/01_reference.arecord.stderr"
  if ! ssh "${PI_USER}@${PI_HOST}" \
       "arecord -D plug:jasper_capture -f S16_LE -r 16000 -c 1 -d ${SECS} -t wav" \
       > "$out" 2> "$stderr_file"; then
    echo "  arecord exited non-zero. stderr:"
    sed 's/^/    /' "$stderr_file"
    exit 1
  fi
  if [[ ! -s "$out" ]]; then
    echo "  arecord produced 0-byte reference (plug:jasper_capture missing or no music playing). stderr:"
    sed 's/^/    /' "$stderr_file"
    exit 1
  fi
  rm -f "$stderr_file"
  echo "    wrote $out"
}

set_bypass() {
  ssh "${PI_USER}@${PI_HOST}" \
    "sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS --values $1 >/dev/null"
  echo "  SHF_BYPASS = $1 ($([ "$1" = "0" ] && echo "chip AEC ON" || echo "chip AEC OFF"))"
}

# ---------- Step 1 ----------
prompt "STEP 1/3: Start music playing through the speaker at production volume.
Use any source (AirPlay, Spotify, BT). Wait 30+ s for stable, full-level music.
The script will then run two captures in series: reference (10 s into music),
then the selected chip channel with chip AEC ON.

NOTE: switching daemon to --ref-only for the chip-mic captures (daemon's
mic-pump would otherwise hold hw:CARD=Array,DEV=0 exclusively → arecord
EBUSY). Wake detection in jasper-voice is suspended for the duration of
Phase 4 as a side effect — this is fine; we're recording, not waking."

echo
echo "==> Switching daemon to --ref-only (releases chip capture for arecord)"
daemon_set_mode ref-only

set_bypass 0
capture_ref
capture_chip_ch "03_mic_aec_on (chip ch${MIC_CHANNEL}, SHF_BYPASS=0)" "03_mic_aec_on.wav" "$MIC_CHANNEL"

# ---------- Step 2 ----------
prompt "STEP 2/3: KEEP MUSIC PLAYING.
We'll flip SHF_BYPASS=1 (chip AEC bypassed) and re-capture the same chip channel.
This is the apples-to-apples comparison against the previous capture."

set_bypass 1
capture_chip_ch "02_mic_aec_off (chip ch${MIC_CHANNEL}, SHF_BYPASS=1)" "02_mic_aec_off.wav" "$MIC_CHANNEL"
set_bypass 0
echo "  restored SHF_BYPASS=0 (chip AEC ON)"

# ---------- Step 3 ----------
prompt "STEP 3/3: STOP music NOW.
Wait for full silence (~10 s).
During the next ${SECS} s, speak a few sentences:
  'Hey Jarvis. Testing one two three. The quick brown fox jumps over the lazy dog.'
This verifies chip AEC doesn't degrade your voice."

capture_chip_ch "04_speech_only (chip ch${MIC_CHANNEL}, AEC on, no music)" "04_speech_only.wav" "$MIC_CHANNEL"

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
