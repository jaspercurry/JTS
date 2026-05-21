"""Forensic audit of the wake-event corpus.

Validates:
  1. WAV file integrity (format, duration, silence, RMS).
  2. Per-event time alignment between AEC ON and AEC OFF legs via
     cross-correlation in the speech band.
  3. DB schema completeness — which columns are populated vs NULL.
"""
from __future__ import annotations

import sqlite3
import sys
import wave
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import signal


SAMPLE_RATE = 16000
EXPECTED_SEC = 6.0
SILENCE_RMS = 30.0  # int16 — anything below this is effectively silence


def load_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        if sw != 2:
            raise ValueError(f"{path.name}: expected 16-bit, got {sw*8}")
        if ch != 1:
            raise ValueError(f"{path.name}: expected mono, got {ch}-ch")
        data = np.frombuffer(w.readframes(n), dtype=np.int16)
    return data, sr, n


def rms(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


def lag_samples(a: np.ndarray, b: np.ndarray, max_lag: int = 4000) -> int:
    """Cross-correlation lag in samples that maximises correlation
    between a and b, in the speech band. Positive = b is delayed
    relative to a. Limited to ±max_lag samples (~250 ms at 16 kHz)."""
    sos = signal.butter(4, [300, 4000], btype="bandpass", fs=SAMPLE_RATE, output="sos")
    a_f = signal.sosfilt(sos, a.astype(np.float32))
    b_f = signal.sosfilt(sos, b.astype(np.float32))
    n = min(len(a_f), len(b_f))
    a_f, b_f = a_f[:n], b_f[:n]
    if rms(a_f.astype(np.int16)) < SILENCE_RMS or rms(b_f.astype(np.int16)) < SILENCE_RMS:
        return 0
    xc = signal.correlate(a_f - a_f.mean(), b_f - b_f.mean(), mode="full")
    lags = signal.correlation_lags(len(a_f), len(b_f), mode="full")
    mask = np.abs(lags) <= max_lag
    if not mask.any():
        return 0
    peak_idx = int(np.argmax(np.abs(xc[mask])))
    return int(lags[mask][peak_idx])


def main(corpus_dir: Path) -> int:
    if not corpus_dir.is_dir():
        print(f"ERROR: {corpus_dir} not a directory", file=sys.stderr)
        return 1

    db_path = corpus_dir / "wake-events.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print(f"Wake-event corpus audit: {corpus_dir}")
    print("=" * 72)

    # ------- Section 1: per-file WAV checks
    print("\n[1] WAV file integrity")
    wavs = sorted(corpus_dir.glob("*.wav"))
    print(f"  {len(wavs)} WAV files found")
    issues = []
    file_stats = {}  # name → (duration, rms, silent_pct)
    for w in wavs:
        try:
            data, sr, n = load_wav(w)
        except Exception as e:
            issues.append(f"  ✗ {w.name}: {e}")
            continue
        duration = n / sr
        wav_rms = rms(data)
        silent_samples = int(np.sum(np.abs(data) < 50))
        silent_pct = 100.0 * silent_samples / max(n, 1)
        file_stats[w.name] = (duration, wav_rms, silent_pct, sr)

        flags = []
        if sr != SAMPLE_RATE:
            flags.append(f"BAD_RATE({sr})")
        if abs(duration - EXPECTED_SEC) > 0.5:
            flags.append(f"BAD_DUR({duration:.2f}s)")
        if wav_rms < SILENCE_RMS:
            flags.append(f"NEAR_SILENT(rms={wav_rms:.1f})")
        if silent_pct > 90:
            flags.append(f"MOSTLY_ZEROS({silent_pct:.1f}%)")
        if flags:
            issues.append(f"  ⚠ {w.name}: {', '.join(flags)} (rms={wav_rms:.0f}, dur={duration:.2f}s)")

    if issues:
        print("  Issues:")
        for i in issues:
            print(i)
    else:
        print(f"  ✓ all {len(wavs)} files have correct format + non-silent content")

    # ------- Section 2: per-event leg parity
    print("\n[2] Per-event AEC ON vs AEC OFF parity")
    rows = list(conn.execute(
        "SELECT event_id, trigger_kind, peak_score_aec_on, peak_score_aec_off, "
        "audio_on_path, audio_off_path FROM wake_events ORDER BY ts_utc"
    ))
    dual_events = []
    single_events = []
    no_audio = []
    for r in rows:
        on = r["audio_on_path"]
        off = r["audio_off_path"]
        if on and on != "rolled_off" and off and off != "rolled_off":
            dual_events.append(r)
        elif on and on != "rolled_off":
            single_events.append(r)
        else:
            no_audio.append(r)
    print(f"  dual-leg events:   {len(dual_events)}")
    print(f"  AEC-ON-only:       {len(single_events)} (pre-fix or single-stream mode)")
    print(f"  no audio (rolled): {len(no_audio)}")

    print("\n  Cross-leg analysis (dual events only):")
    print(f"  {'event_id':<26} {'dur_on':>7} {'dur_off':>7} {'rms_on':>7} {'rms_off':>7} {'lag_ms':>8} {'trigger':<14}")
    print(f"  {'-'*26} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*14}")
    alignment_issues = []
    for r in dual_events:
        on_stats = file_stats.get(r["audio_on_path"])
        off_stats = file_stats.get(r["audio_off_path"])
        if not on_stats or not off_stats:
            print(f"  ⚠ {r['event_id']}: missing file_stats")
            continue
        dur_on, rms_on, _, _ = on_stats
        dur_off, rms_off, _, _ = off_stats
        on_data, _, _ = load_wav(corpus_dir / r["audio_on_path"])
        off_data, _, _ = load_wav(corpus_dir / r["audio_off_path"])
        lag = lag_samples(on_data, off_data)
        lag_ms = 1000.0 * lag / SAMPLE_RATE
        print(
            f"  {r['event_id']:<26} {dur_on:>6.2f}s {dur_off:>6.2f}s "
            f"{rms_on:>7.0f} {rms_off:>7.0f} {lag_ms:>+7.1f}  {r['trigger_kind']:<14}"
        )
        # Flag concerns
        if abs(dur_on - dur_off) > 0.05:
            alignment_issues.append(
                f"    ✗ {r['event_id']}: duration mismatch on={dur_on:.2f} off={dur_off:.2f}"
            )
        if abs(lag_ms) > 250:
            alignment_issues.append(
                f"    ⚠ {r['event_id']}: large cross-leg lag {lag_ms:+.0f} ms"
            )

    if alignment_issues:
        print("\n  Alignment issues:")
        for i in alignment_issues:
            print(i)
    else:
        print("\n  ✓ all dual-leg events durations match within 50 ms")
        print("  ✓ all cross-leg lags within ±250 ms (typical speech-band xcorr tolerance)")

    # ------- Section 3: DB completeness — which schema fields are populated
    print("\n[3] DB schema completeness")
    cur = conn.execute("SELECT * FROM wake_events LIMIT 1")
    cols = [d[0] for d in cur.description]
    populated = {c: 0 for c in cols}
    total = 0
    for r in conn.execute("SELECT * FROM wake_events"):
        total += 1
        for c in cols:
            if r[c] is not None:
                populated[c] += 1

    print(f"  Total events: {total}")
    print(f"  {'column':<26} {'populated':>10} {'pct':>6}")
    print(f"  {'-'*26} {'-'*10} {'-'*6}")
    for c in cols:
        p = populated[c]
        pct = 100.0 * p / max(total, 1)
        marker = "  " if p == total else "⚠ " if 0 < p < total else "✗ "
        print(f"  {marker}{c:<24} {p:>10} {pct:>5.0f}%")

    print("\n  Legend: ✓=all rows populated, ⚠=partial, ✗=never populated")
    print("  (NULL is correct for ts_late_cancel etc. on events that didn't reach that stage)")
    print(f"  (NULL audio_*_path is correct on '{single_events[0]['event_id']}' style events if pre-fix)" if single_events else "")

    conn.close()
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("wake-events/latest")
    sys.exit(main(target))
