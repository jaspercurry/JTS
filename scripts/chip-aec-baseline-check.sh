#!/usr/bin/env bash
# Phase 2: baseline gate before judging chip-AEC convergence.
#
# This runs after chip-aec-setup.sh and before chip-aec-poll-convergence.sh.
# It answers the boring-but-load-bearing questions first:
#   - Is the experiment daemon feeding the XVF3800 USB-IN endpoint?
#   - Is the reference tap non-silent and unclipped?
#   - Does the chip mic hear the speaker echo with AEC bypassed?
#   - Is the measured ref->mic lag repeatable enough to start from a
#     real AUDIO_MGR_SYS_DELAY candidate instead of guessing?
#
# Outputs land in captures/chip-aec-baseline/<timestamp>/ on the laptop.
#
# Usage:
#   bash scripts/chip-aec-baseline-check.sh
#
# Optional env:
#   REPEATS=3          number of repeated captures
#   SECS=6             seconds per capture
#   GAP=8              seconds between repeated captures
#   MAX_LAG=4000       cross-correlation search window at 16 kHz
#   STIMULUS=chirp     chirp injects a calibration signal; none uses ambient music only
#   REF_DELAY_MS=0      upstream delay applied by chip_aec_experiment reference feeder
#   PYTHON_BIN=python3 analyzer Python (must have numpy)
#   ASSUME_READY=1     skip interactive "music is playing" prompt

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
REPEATS="${REPEATS:-3}"
SECS="${SECS:-6}"
GAP="${GAP:-8}"
MAX_LAG="${MAX_LAG:-4000}"
STIMULUS="${STIMULUS:-chirp}"
REF_DELAY_MS="${REF_DELAY_MS:-${JASPER_CHIP_AEC_REF_DELAY_MS:-0}}"
MIC_CHANNEL="${MIC_CHANNEL:-${JASPER_CHIP_AEC_MIC_CHANNEL:-0}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

TS="$(date +%Y%m%d-%H%M%S)"
OUTDIR="captures/chip-aec-baseline/${TS}"
mkdir -p "$OUTDIR"

RESTORE_NEEDED=0

prompt() {
  if [[ "${ASSUME_READY:-0}" = "1" ]]; then
    return
  fi
  echo
  echo "------------------------------------------"
  echo "$@"
  echo "------------------------------------------"
  read -r -p "Press Enter when ready... " _ < /dev/tty
}

ssh_pi() {
  ssh "${PI_USER}@${PI_HOST}" "$@"
}

set_bypass() {
  local value="$1"
  ssh_pi "sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS --values ${value} >/dev/null"
  echo "  SHF_BYPASS = ${value} ($([[ "${value}" = "0" ]] && echo "chip AEC ON" || echo "chip AEC bypassed"))"
}

daemon_set_mode() {
  local mode="$1"
  local extra=""
  if [[ "$mode" = "ref-only" ]]; then
    extra="--ref-only"
  fi

  ssh_pi "set -e
    module='jasper.chip_aec'_experiment
    module_re='[j]asper[.]chip_aec_experiment'
    boundary=\$(wc -l < /var/log/chip-aec-experiment.log 2>/dev/null || echo 0)
    sudo pkill -f \"\$module_re\" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f \"\$module_re\" >/dev/null || break
      sleep 0.4
    done
    if pgrep -f \"\$module_re\" >/dev/null; then
      echo '    old daemon did not exit after SIGTERM; sending SIGKILL'
      sudo pkill -9 -f \"\$module_re\" || true
      sleep 0.5
    fi
    sudo bash -c \"nohup /opt/jasper/.venv/bin/python -m \$module --ref-delay-ms '${REF_DELAY_MS}' --mic-channel '${MIC_CHANNEL}' ${extra} >> /var/log/chip-aec-experiment.log 2>&1 < /dev/null &\"
    sleep 2
    if ! pgrep -f \"\$module_re\" >/dev/null; then
      echo '    FAILED to restart daemon in ${mode} mode - last 20 log lines:'
      sudo tail -20 /var/log/chip-aec-experiment.log
      exit 1
    fi
    new_lines=\$(sudo tail -n +\$((boundary + 1)) /var/log/chip-aec-experiment.log 2>/dev/null || true)
    if echo \"\$new_lines\" | grep -qE '(ref feeder|mic pump) open failed'; then
      echo '    daemon process alive but PCM open failed - log excerpt:'
      echo \"\$new_lines\" | grep -E 'open failed' | head -5
      exit 1
    fi
    echo \"    daemon now in ${mode} mode (PID \$(pgrep -f \"\$module_re\"))\"
  "
}

cleanup() {
  local rc=$?
  if [[ "$RESTORE_NEEDED" = "1" ]]; then
    echo
    echo "==> Restoring chip-AEC experiment daemon for convergence testing"
    set +e
    set_bypass 0
    daemon_set_mode full
    set -e
  fi
  exit "$rc"
}
trap cleanup EXIT

capture_run() {
  local idx="$1"
  local run_label
  run_label="$(printf 'run%02d' "$idx")"
  local remote_dir="/tmp/chip-aec-baseline-${TS}-${run_label}"

  echo
  echo "==> Capture ${idx}/${REPEATS}: ${SECS}s simultaneous ref + chip mic"
  ssh_pi "set -euo pipefail
    rm -rf '${remote_dir}'
    mkdir -p '${remote_dir}'

    if [[ '${STIMULUS}' = 'chirp' ]]; then
      /opt/jasper/.venv/bin/python - <<'PYREMOTE'
import wave
from pathlib import Path

import numpy as np

path = Path('${remote_dir}/stimulus.wav')
fs = 48000
total_s = 2.4
marker_s = 0.22
starts = (0.15, 0.85, 1.55)
f0 = 450.0
f1 = 4200.0
amp = 0.26
data = np.zeros(int(total_s * fs), dtype=np.float64)
marker_n = int(marker_s * fs)
t = np.arange(marker_n, dtype=np.float64) / fs
k = (f1 - f0) / marker_s
phase = 2.0 * np.pi * (f0 * t + 0.5 * k * t * t)
marker = np.sin(phase)
fade = int(0.006 * fs)
marker[:fade] *= np.linspace(0.0, 1.0, fade)
marker[-fade:] *= np.linspace(1.0, 0.0, fade)
for start in starts:
    offset = int(start * fs)
    data[offset:offset + marker_n] += marker
data = np.clip(data * amp, -1.0, 1.0)
pcm = (data * 32767.0).astype(np.int16)
stereo = np.column_stack([pcm, pcm]).reshape(-1)
with wave.open(str(path), 'wb') as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(fs)
    w.writeframes(stereo.tobytes())
PYREMOTE
    fi

    arecord -q -D plug:jasper_capture -f S16_LE -r 16000 -c 1 -d '${SECS}' -t wav '${remote_dir}/ref.wav' >'${remote_dir}/ref.stderr' 2>&1 &
    ref_pid=\$!
    arecord -q -D hw:CARD=Array,DEV=0 -f S16_LE -r 16000 -c 6 -d '${SECS}' -t wav '${remote_dir}/mic6.wav' >'${remote_dir}/mic.stderr' 2>&1 &
    mic_pid=\$!

    if [[ '${STIMULUS}' = 'chirp' ]]; then
      sleep 0.4
      if ! aplay -q -D correction_substream '${remote_dir}/stimulus.wav' >'${remote_dir}/stimulus.stderr' 2>&1; then
        echo 'stimulus aplay stderr:'
        sed 's/^/  stimulus: /' '${remote_dir}/stimulus.stderr' || true
        exit 1
      fi
    fi

    ref_rc=0
    mic_rc=0
    wait \$ref_pid || ref_rc=\$?
    wait \$mic_pid || mic_rc=\$?

    if [[ \$ref_rc -ne 0 || \$mic_rc -ne 0 ]]; then
      echo 'reference arecord stderr:'
      sed 's/^/  ref: /' '${remote_dir}/ref.stderr' || true
      echo 'mic arecord stderr:'
      sed 's/^/  mic: /' '${remote_dir}/mic.stderr' || true
      exit 1
    fi
    test -s '${remote_dir}/ref.wav'
    test -s '${remote_dir}/mic6.wav'
  "

  ssh_pi "cat '${remote_dir}/ref.wav'" > "${OUTDIR}/${run_label}_ref.wav"
  ssh_pi "cat '${remote_dir}/mic6.wav'" > "${OUTDIR}/${run_label}_mic6.wav"
  ssh_pi "rm -rf '${remote_dir}'"
  echo "    wrote ${OUTDIR}/${run_label}_ref.wav"
  echo "    wrote ${OUTDIR}/${run_label}_mic6.wav"
}

echo "=== chip-aec-experiment: baseline gate ==="
echo "Pi: ${PI_USER}@${PI_HOST}"
echo "Output: ${OUTDIR}/"
echo "Repeats: ${REPEATS}  Capture length: ${SECS}s  Gap: ${GAP}s  Stimulus: ${STIMULUS}  Ref delay: ${REF_DELAY_MS} ms  Mic channel: ${MIC_CHANNEL}"
echo

echo "==> Route sanity"
ssh_pi "set -e
  pgrep -f '[j]asper.chip_aec_experiment' >/dev/null
  echo '  daemon PID(s):' \$(pgrep -f '[j]asper.chip_aec_experiment')
  echo '  chip USB-IN playback status:'
  awk '/^Playback:/{p=1} p && /Status:/{print \"    \" \$0; exit}' /proc/asound/Array/stream0 || true
  echo '  recent feeder log:'
  sudo tail -40 /var/log/chip-aec-experiment.log 2>/dev/null | grep -E 'ref feeder|open failed|unhandled|underrun|write error' | tail -8 | sed 's/^/    /' || true
"

prompt "Start steady music through the speaker now.
Use the source and volume you want to test. Keep it playing through all
${REPEATS} baseline captures. The script will temporarily bypass chip AEC,
measure the reference and mic echo, then restore chip AEC for convergence."

echo
echo "==> Switching daemon to --ref-only and bypassing chip AEC for measurement"
RESTORE_NEEDED=1
daemon_set_mode ref-only
set_bypass 1

for i in $(seq 1 "$REPEATS"); do
  capture_run "$i"
  if [[ "$i" != "$REPEATS" ]]; then
    echo "    waiting ${GAP}s before repeat..."
    sleep "$GAP"
  fi
done

echo
echo "==> Analyzing baseline captures"
"$PYTHON_BIN" - "$OUTDIR" "$REPEATS" "$MAX_LAG" "$REF_DELAY_MS" <<'PYEOF'
from __future__ import annotations

import json
import math
import sys
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:
    print("ERROR: numpy is required for the delay analyzer.", file=sys.stderr)
    print("Set PYTHON_BIN to a Python with numpy installed.", file=sys.stderr)
    raise SystemExit(1) from exc


outdir = Path(sys.argv[1])
repeats = int(sys.argv[2])
max_lag = int(sys.argv[3])
upstream_ref_delay_ms = float(sys.argv[4])
rate = 16000
upstream_ref_delay_samples = int(round(upstream_ref_delay_ms * rate / 1000.0))


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        channels = w.getnchannels()
        wav_rate = w.getframerate()
        raw = w.readframes(frames)
    arr = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        arr = arr.reshape(-1, channels)
    return arr, wav_rate, channels


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1.0) / 32768.0)


def level(samples: np.ndarray) -> dict[str, float | int]:
    mono = samples.astype(np.float64).reshape(-1)
    if mono.size == 0:
        return {"rms": 0.0, "rms_dbfs": -120.0, "peak": 0, "peak_dbfs": -120.0, "clip_samples": 0}
    rms = float(np.sqrt(np.mean((mono - np.mean(mono)) ** 2)))
    peak = int(np.max(np.abs(mono)))
    clips = int(np.sum(np.abs(mono) >= 32760))
    return {
        "rms": rms,
        "rms_dbfs": dbfs(rms),
        "peak": peak,
        "peak_dbfs": dbfs(float(peak)),
        "clip_samples": clips,
    }


def condition(samples: np.ndarray) -> np.ndarray:
    x = samples.astype(np.float64).reshape(-1)
    if x.size > rate:
        trim = min(rate // 4, x.size // 10)
        x = x[trim:-trim]
    x = x - np.mean(x)
    # First-difference emphasizes edges and reduces room rumble / DC.
    x = np.diff(x, prepend=x[0])
    energy = np.sqrt(np.sum(x * x))
    if energy <= 0:
        return x
    return x / energy


def correlate_fft(mic: np.ndarray, ref: np.ndarray) -> tuple[int, float]:
    a = condition(mic)
    b = condition(ref)
    n = len(a) + len(b) - 1
    size = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fa * np.conj(fb), size)
    full = np.concatenate((corr[-(len(b) - 1):], corr[:len(a)]))
    center = len(b) - 1
    lo = max(0, center - max_lag)
    hi = min(len(full), center + max_lag + 1)
    window = full[lo:hi]
    idx = int(np.argmax(np.abs(window)))
    lag = (lo + idx) - center
    confidence = float(abs(window[idx]))
    return int(lag), confidence


summary: dict[str, object] = {"runs": []}
best_lags: list[int] = []
channel_lags: dict[int, list[int]] = {}
channel_confs: dict[int, list[float]] = {}
valid = True

print()
print("Per-run measurements")
print("--------------------")
for i in range(1, repeats + 1):
    label = f"run{i:02d}"
    ref, ref_rate, ref_channels = read_wav(outdir / f"{label}_ref.wav")
    mic6, mic_rate, mic_channels = read_wav(outdir / f"{label}_mic6.wav")
    if ref_rate != rate or mic_rate != rate:
        raise SystemExit(f"{label}: expected 16 kHz WAVs, got ref={ref_rate}, mic={mic_rate}")
    if ref_channels != 1 or mic_channels != 6:
        raise SystemExit(f"{label}: expected ref mono + mic 6ch, got ref={ref_channels}, mic={mic_channels}")

    ref_lvl = level(ref)
    ch_results = []
    for ch in range(mic_channels):
        mic = mic6[:, ch]
        mic_lvl = level(mic)
        lag, conf = correlate_fft(mic, ref)
        channel_lags.setdefault(ch, []).append(int(lag))
        channel_confs.setdefault(ch, []).append(float(conf))
        residual_lag = int(lag) - upstream_ref_delay_samples
        ch_results.append(
            {
                "channel": ch,
                "lag_samples": lag,
                "lag_ms": lag * 1000.0 / rate,
                "residual_lag_samples": residual_lag,
                "residual_lag_ms": residual_lag * 1000.0 / rate,
                "confidence": conf,
                "level": mic_lvl,
            }
        )

    best = max(ch_results, key=lambda r: float(r["confidence"]))
    best_lags.append(int(best["lag_samples"]))
    run = {
        "label": label,
        "reference": ref_lvl,
        "channels": ch_results,
        "best_channel": best["channel"],
        "best_lag_samples": best["lag_samples"],
        "best_confidence": best["confidence"],
    }
    summary["runs"].append(run)

    print(
        f"{label}: ref RMS={ref_lvl['rms']:7.1f} ({ref_lvl['rms_dbfs']:+5.1f} dBFS) "
        f"peak={ref_lvl['peak']:5d} ({ref_lvl['peak_dbfs']:+5.1f} dBFS) clips={ref_lvl['clip_samples']}"
    )
    for ch in ch_results:
        lvl = ch["level"]
        print(
            f"  ch{ch['channel']}: lag={ch['lag_samples']:5d} "
            f"({ch['lag_ms']:+7.2f} ms) residual={ch['residual_lag_samples']:5d} "
            f"({ch['residual_lag_ms']:+7.2f} ms) conf={ch['confidence']:.5f} "
            f"RMS={lvl['rms']:7.1f} ({lvl['rms_dbfs']:+5.1f} dBFS) "
            f"peak={lvl['peak']:5d} clips={lvl['clip_samples']}"
        )
    print(f"  best: ch{best['channel']} lag={best['lag_samples']} samples")

    if float(ref_lvl["rms"]) < 50.0:
        print(f"  ERROR: {label} reference is near-silent; route/music invalid.")
        valid = False
    if int(ref_lvl["clip_samples"]) > 0:
        print(f"  WARN: {label} reference has clipped samples.")
    if float(best["confidence"]) < 0.003:
        print(f"  WARN: {label} correlation confidence is weak; delay may be noisy.")

print()
print("Repeatability")
print("-------------")
channel_stats = []
for ch, lags in sorted(channel_lags.items()):
    median_ch = int(round(float(np.median(lags))))
    spread_ch = int(max(lags) - min(lags))
    avg_conf = float(np.mean(channel_confs.get(ch, [0.0])))
    channel_stats.append(
        {
            "channel": ch,
            "lags": lags,
            "median": median_ch,
            "spread": spread_ch,
            "avg_confidence": avg_conf,
        }
    )

if not channel_stats:
    print("ERROR: no lags measured")
    valid = False
    median = 0
    spread = 0
else:
    eligible = [s for s in channel_stats if float(s["avg_confidence"]) >= 0.02]
    if not eligible:
        eligible = channel_stats
    selected = min(eligible, key=lambda s: (int(s["spread"]), -float(s["avg_confidence"])))
    median = int(selected["median"])
    spread = int(selected["spread"])
    selected_lags = [int(v) for v in selected["lags"]]
    mad = float(np.median(np.abs(np.array(selected_lags) - median)))
    print(f"upstream ref delay: {upstream_ref_delay_samples} samples ({upstream_ref_delay_ms:.2f} ms)")
    print(f"best-channel source-relative lags: {best_lags}")
    print("per-channel lag stability:")
    for stat in channel_stats:
        residuals = [int(v) - upstream_ref_delay_samples for v in stat["lags"]]
        print(
            f"  ch{stat['channel']}: lags={stat['lags']} "
            f"residuals={residuals} "
            f"spread={stat['spread']} samples "
            f"avg_conf={stat['avg_confidence']:.5f}"
        )
    print(f"selected channel: ch{selected['channel']}")
    source_median = median
    median = source_median - upstream_ref_delay_samples
    print(f"selected source-relative lag: {source_median} samples ({source_median * 1000.0 / rate:+.2f} ms)")
    print(f"candidate AUDIO_MGR_SYS_DELAY: {median} samples ({median * 1000.0 / rate:+.2f} ms)")
    print(f"spread: {spread} samples ({spread * 1000.0 / rate:.2f} ms), MAD={mad:.1f} samples")
    if spread <= 80:
        print("status: OK - repeatability is tight enough for a first SYS_DELAY candidate.")
    elif spread <= 240:
        print("status: CAUTION - repeatability is loose; re-run with louder/broader music before trusting this.")
    else:
        print("status: FAIL - delay is moving too much for a clean chip-AEC verdict.")
        valid = False
    if median < -64 or median > 256:
        print("status: FAIL - residual delay is outside firmware read-back-confirmed SYS_DELAY range [-64, +256].")
        valid = False

summary["candidate_sys_delay_samples"] = median
summary["candidate_sys_delay_ms"] = median * 1000.0 / rate
summary["upstream_ref_delay_samples"] = upstream_ref_delay_samples
summary["upstream_ref_delay_ms"] = upstream_ref_delay_ms
summary["lag_spread_samples"] = spread
summary["channel_stability"] = channel_stats
summary["valid"] = valid
(outdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print()
print(f"wrote {outdir / 'summary.json'}")

if not valid:
    raise SystemExit(2)
PYEOF

echo
echo "=== Baseline complete ==="
echo "Files: ${OUTDIR}/"
echo
echo "If the candidate delay looks sane, apply it before polling convergence:"
echo "  ssh ${PI_USER}@${PI_HOST} 'sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY --values <samples>'"
echo
echo "Then run:"
echo "  bash scripts/chip-aec-poll-convergence.sh"
