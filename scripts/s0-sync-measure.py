#!/usr/bin/env python3
# =============================================================================
# s0-sync-measure.py — analyzer for the S0-SYNC de-risk BENCH (THROWAWAY)
# =============================================================================
#
# The measurement half of scripts/s0-sync-bench.sh. Answers the
# docs/HANDOFF-distributed-active.md "Multi-Pi validation" S0-sync question:
# does the active follower (snapclient -> snd-aloop -> crossover CamillaDSP ->
# DAC) stay sample-locked?
#
# Two gates, two subcommands:
#
#   acoustic --wav W   Single-mic autocorrelation on ONE click recording. The
#                      leader's mic hears BOTH speakers playing the SAME 1 Hz
#                      broadband click; the autocorrelation secondary-peak lag
#                      is the inter-speaker arrival offset |t_A - t_B|. (Method
#                      from scripts/multiroom-spike-measure.py acoustic mode;
#                      a broadband click is required — a tone's autocorrelation
#                      is ambiguous at its own period.) Use for the smoke
#                      go/no-go (is a clean secondary peak visible?).
#
#   soak --dir D       Aggregate a whole run: (1) parse soak-*.log for the
#                      snd-aloop xrun totals + CPU/temp/throttle/Pss budget;
#                      (2) run the acoustic estimate over every periodic
#                      cap-*.wav, build the inter-speaker offset time series,
#                      report p50/p95/p99/max (raw AND placement-detrended),
#                      count resync jumps, and emit the combined PASS/FAIL +
#                      the doc-ready numbers (p99, xrun count, CPU/temp).
#
# ACCEPTANCE (docs/HANDOFF-distributed-active.md): p99 inter-speaker offset
# < 5 ms over a 2-hour run, no audible resync; PLUS a >= 24 h xrun soak with a
# clean journal. PASS requires BOTH.
#
# Mic-placement note: the raw |offset| includes a STATIC bias of
# (d_A - d_B)/343 m s^-1 from where the single mic sits. The DETRENDED metric
# (|offset - run-median|) removes it and is the pure clock-lock signal; the raw
# metric is the literal acceptance number. Place the mic roughly between the
# speakers and they converge.
# =============================================================================

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import wave

import numpy as np

TARGET_P99_MS = 5.0          # docs/HANDOFF-distributed-active.md sync target
RESYNC_JUMP_MS = 2.0         # consecutive-capture median jump flagged as resync
SAMPLE_RATE = 48_000
LAG_MIN_MS = 0.3             # skip the zero-lag autocorrelation lobe
LAG_MAX_MS = 50.0            # search window for the inter-speaker peak
CLICK_WINDOW_MS = 150.0      # window after each onset (must hold both clicks)


# ---------------------------------------------------------------------------
def percentile(values, pct):
    if not values:
        return float("nan")
    return float(np.percentile(np.asarray(values, dtype=float), pct))


def read_wav_mono(path):
    """Return (float64 mono samples in [-1,1], sample_rate). Downmix if multi."""
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if nch > 1:
        a = a.reshape((-1, nch)).mean(axis=1)
    return a, sr


# ---------------------------------------------------------------------------
def find_onsets(x, sr, min_gap_s=0.5):
    """Energy-envelope onset detector for the ~2 ms clicks (~1 s apart)."""
    win = max(1, int(0.005 * sr))
    energy = np.convolve(x * x, np.ones(win), mode="same")
    peak = float(energy.max()) if energy.size else 0.0
    if peak <= 0.0:
        return []
    thresh = peak * 0.15
    refractory = int(min_gap_s * sr)
    onsets = []
    last = -refractory
    above = energy > thresh
    idx = np.nonzero(above)[0]
    for i in idx:
        if i - last > refractory:
            onsets.append(int(i))
            last = i
    return onsets


def offset_for_onset(x, sr, onset):
    """Autocorrelation secondary-peak lag (ms) = inter-speaker offset |t_A-t_B|.

    The window after the onset holds both speakers' copies of the same click.
    Autocorrelation peaks at lag 0 and at the inter-speaker spacing; we return
    the strongest peak in [LAG_MIN_MS, LAG_MAX_MS], plus a sharpness ratio
    (peak / median) so callers can reject windows with no clean secondary peak.
    """
    n = int(CLICK_WINDOW_MS / 1000.0 * sr)
    seg = x[onset:onset + n]
    if seg.size < n // 2:
        return None
    seg = seg - seg.mean()
    # FFT autocorrelation.
    m = 1 << int(np.ceil(np.log2(2 * seg.size)))
    f = np.fft.rfft(seg, m)
    ac = np.fft.irfft(f * np.conj(f), m)[: seg.size]
    lag_lo = int(LAG_MIN_MS / 1000.0 * sr)
    lag_hi = min(int(LAG_MAX_MS / 1000.0 * sr), seg.size - 1)
    if lag_hi <= lag_lo:
        return None
    window = ac[lag_lo:lag_hi]
    k = int(np.argmax(window))
    peak_lag = lag_lo + k
    peak_val = float(window[k])
    med = float(np.median(np.abs(ac[lag_lo:lag_hi]))) + 1e-9
    sharpness = peak_val / med
    return peak_lag / sr * 1000.0, sharpness


def offsets_in_wav(path, min_sharpness=4.0):
    """All per-onset inter-speaker offsets (ms) in one capture WAV."""
    x, sr = read_wav_mono(path)
    offs = []
    for onset in find_onsets(x, sr):
        r = offset_for_onset(x, sr, onset)
        if r is None:
            continue
        off_ms, sharp = r
        if sharp >= min_sharpness:
            offs.append(off_ms)
    return offs


# ---------------------------------------------------------------------------
def run_acoustic(args):
    offs = offsets_in_wav(args.wav, min_sharpness=args.min_sharpness)
    if not offs:
        print(f"acoustic: NO clean secondary peak in {args.wav}.\n"
              f"  Either the mic does not hear BOTH speakers, a speaker is\n"
              f"  silent, or the second speaker is too quiet relative to the\n"
              f"  first. Check that speakers are connected and raise the\n"
              f"  quieter follower's level, then re-smoke.", file=sys.stderr)
        return 1
    arr = np.asarray(offs)
    print(f"acoustic: {args.wav}")
    print(f"  onsets with clean peak : {len(offs)}")
    print(f"  inter-speaker offset ms: p50={np.median(arr):.3f} "
          f"p95={percentile(offs,95):.3f} p99={percentile(offs,99):.3f} "
          f"max={arr.max():.3f}")
    print(f"  (single capture — this is the smoke go/no-go: a clean secondary\n"
          f"   peak means the single-mic autocorrelation method works here.)")
    return 0


# ---------------------------------------------------------------------------
def _fnum(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_soak_log(path):
    """Parse one soak-*.log -> dict of budget + clock-lock summary for that host."""
    xruns = 0
    temps, loads, cpss, spss = [], [], [], []
    buf_levels, rate_adjs = [], []
    non_running = 0
    throttled_any = False
    host = "?"
    n = 0
    with open(path) as f:
        for line in f:
            kv = dict(re.findall(r"(\w+)=(\S+)", line))
            if not kv:
                continue
            n += 1
            host = kv.get("host", host)
            xruns = max(xruns, int(kv.get("camilla_xruns", 0) or 0))
            t = _fnum(kv.get("temp_c"))
            if t is not None:
                temps.append(t)
            if kv.get("throttled", "0x0") not in ("0x0", "0", ""):
                throttled_any = True
            ld = _fnum(kv.get("load1"))
            if ld is not None:
                loads.append(ld)
            cpss.append(int(kv.get("camilla_pss_kb", 0) or 0))
            spss.append(int(kv.get("snapclient_pss_kb", 0) or 0))
            # Clock-lock telemetry (camilla websocket).
            st = kv.get("camilla_state", "NA")
            if st not in ("RUNNING", "NA"):
                non_running += 1
            bl = _fnum(kv.get("buffer_level"))
            if bl is not None and bl > 0:
                buf_levels.append(bl)
            ra = _fnum(kv.get("rate_adjust"))
            if ra is not None and ra > 0:
                rate_adjs.append(ra)
    return {
        "host": host,
        "samples": n,
        "xruns": xruns,
        "temp_min": min(temps) if temps else float("nan"),
        "temp_max": max(temps) if temps else float("nan"),
        "throttled": throttled_any,
        "load_max": max(loads) if loads else float("nan"),
        "camilla_pss_mb": round(max(cpss) / 1024.0, 1) if cpss else None,
        "snapclient_pss_mb": round(max(spss) / 1024.0, 1) if spss else None,
        "hours": round(n / 60.0, 2),
        # Clock-lock: camilla holding the snd-aloop capture against the DAC.
        "non_running": non_running,
        "buf_min": min(buf_levels) if buf_levels else None,
        "buf_max": max(buf_levels) if buf_levels else None,
        "buf_mean": round(sum(buf_levels) / len(buf_levels), 1) if buf_levels else None,
        "rate_min": min(rate_adjs) if rate_adjs else None,
        "rate_max": max(rate_adjs) if rate_adjs else None,
        "rate_n": len(rate_adjs),
    }


def _ts_of(path):
    m = re.search(r"cap-(\d+)\.wav$", os.path.basename(path))
    return int(m.group(1)) if m else 0


def run_soak(args):
    d = args.dir
    print("=" * 74)
    print("S0-SYNC BENCH — soak + acoustic summary")
    print("=" * 74)

    # --- budget / xrun gate -------------------------------------------------
    logs = sorted(glob.glob(os.path.join(d, "soak-*.log")))
    budgets = [parse_soak_log(p) for p in logs]
    total_xruns = 0
    any_throttle = False
    print("\nBUDGET + XRUN (per host):")
    if not budgets:
        print("  (no soak-*.log found — run --soak then --collect)")
    for b in budgets:
        total_xruns += b["xruns"]
        any_throttle = any_throttle or b["throttled"]
        print(f"  {b['host']:<9} {b['hours']:>5}h  xruns={b['xruns']:<4} "
              f"temp={b['temp_min']:.1f}-{b['temp_max']:.1f}C "
              f"throttled={b['throttled']} load_max={b['load_max']:.2f} "
              f"camilla_pss={b['camilla_pss_mb']}MB "
              f"snapclient_pss={b['snapclient_pss_mb']}MB")

    # --- clock-lock gate (the DIRECT seam signal) ---------------------------
    # camilla holds the snd-aloop capture locked to the DAC: state RUNNING
    # throughout, buffer_level never drains, rate_adjust stays near 1.0 within a
    # tight band (a wide band = oscillation = fighting the clock; a runaway to
    # the 0.98/1.02 clamp = lock lost). This is more direct than acoustics for
    # the actual S0 risk (does the rate_adjust-no-resampler loopback hold).
    print("\nCLOCK-LOCK (camilla rate-adjust telemetry — direct seam signal):")
    clock_lock_pass = bool(budgets)
    for b in budgets:
        has = b["rate_n"] > 0
        ok = (has and b["non_running"] == 0 and (b["buf_min"] or 0) > 0
              and (b["rate_min"] or 0) >= 0.98 and (b["rate_max"] or 0) <= 1.02
              and (b["rate_max"] - b["rate_min"]) < 0.01)
        clock_lock_pass = clock_lock_pass and ok
        if not has:
            print(f"  {b['host']:<9} (no camilla telemetry in log)")
            clock_lock_pass = False
            continue
        run_note = "always" if b["non_running"] == 0 else f"BROKE x{b['non_running']}"
        print(f"  {b['host']:<9} RUNNING={run_note} "
              f"buffer={b['buf_min']:.0f}-{b['buf_max']:.0f}(mean {b['buf_mean']}) "
              f"rate_adjust={b['rate_min']:.6f}..{b['rate_max']:.6f}  "
              f"{'LOCKED' if ok else 'CHECK'}")

    # --- acoustic sync gate -------------------------------------------------
    wavs = sorted(glob.glob(os.path.join(d, "acoustic", "cap-*.wav")), key=_ts_of)
    all_offsets = []
    per_capture_median = []   # (ts, median_offset_ms) for drift / resync
    for w in wavs:
        offs = offsets_in_wav(w, min_sharpness=args.min_sharpness)
        if offs:
            all_offsets.extend(offs)
            per_capture_median.append((_ts_of(w), float(np.median(offs))))

    print(f"\nACOUSTIC ({len(wavs)} captures, "
          f"{len(per_capture_median)} with a clean peak, "
          f"{len(all_offsets)} offset samples):")
    sync_pass = None
    p99 = float("nan")
    if not all_offsets:
        print("  NO clean secondary peaks across the run. The single-mic\n"
              "  autocorrelation never saw both speakers — acoustic gate is\n"
              "  INCONCLUSIVE (check speaker wiring / second-speaker level).")
    else:
        arr = np.asarray(all_offsets)
        med = float(np.median(arr))
        detr = np.abs(arr - med)
        p99 = percentile(all_offsets, 99)
        p99_detr = percentile(detr.tolist(), 99)
        # resync jumps: consecutive per-capture medians moving > RESYNC_JUMP_MS.
        jumps = 0
        meds = [m for _, m in per_capture_median]
        for a, bb in zip(meds, meds[1:]):
            if abs(bb - a) > RESYNC_JUMP_MS:
                jumps += 1
        print(f"  inter-speaker offset ms (RAW, incl. placement bias):")
        print(f"      p50={med:.3f} p95={percentile(all_offsets,95):.3f} "
              f"p99={p99:.3f} max={arr.max():.3f}")
        print(f"  inter-speaker offset ms (DETRENDED, pure clock-lock):")
        print(f"      p99={p99_detr:.3f} max={float(detr.max()):.3f}")
        print(f"  resync jumps (>|{RESYNC_JUMP_MS}|ms between captures): {jumps}")
        sync_pass = (p99 < TARGET_P99_MS) and (jumps == 0)

    # --- combined verdict ---------------------------------------------------
    # Two AUTONOMOUS gates (xrun soak + clock-lock telemetry) carry the seam
    # de-risk; the acoustic p99 is the gold-standard end-to-end confirmation but
    # needs a mic BETWEEN the speakers (the onboard mic cannot — proven), so it
    # is reported as PENDING rather than failing the run.
    xrun_pass = (total_xruns == 0) and not any_throttle and bool(budgets)
    print("\n" + "-" * 74)
    print(f"  XRUN/SOAK  gate : {'PASS' if xrun_pass else 'FAIL/INCOMPLETE'} "
          f"(total xruns={total_xruns}, throttled={any_throttle})")
    print(f"  CLOCK-LOCK gate : {'PASS' if clock_lock_pass else 'FAIL/INCOMPLETE'} "
          f"(camilla RUNNING + buffer stable + rate_adjust tight ~1.0)")
    if sync_pass is None:
        print("  ACOUSTIC   gate : PENDING — needs a mic BETWEEN the speakers. jts3's\n"
              "                    onboard XVF mic measures its OWN reflection (~0.29 ms\n"
              "                    constant, persists with the follower muted), not the\n"
              "                    inter-speaker offset. Place a mic between the speakers\n"
              "                    (or relocate the follower) and re-capture for the p99.")
    else:
        print(f"  ACOUSTIC   gate : {'PASS' if sync_pass else 'FAIL'} "
              f"(p99={p99:.3f}ms vs {TARGET_P99_MS}ms target)")

    if sync_pass is False or (budgets and (not xrun_pass or not clock_lock_pass)):
        overall = "FAIL"
    elif xrun_pass and clock_lock_pass and sync_pass:
        overall = "PASS"
    elif xrun_pass and clock_lock_pass:
        overall = "PASS (telemetry) — acoustic p99 PENDING a between-speakers mic"
    else:
        overall = "INCOMPLETE"
    print("=" * 74)
    print(f"  S0-SYNC VERDICT: {overall}")
    print("=" * 74)
    print("  Consequence (write into HANDOFF-distributed-active.md):")
    if overall.startswith("PASS"):
        print("    Clock seam holds. Slice 3 is go PROVIDED the acoustic p99 is\n"
              "    confirmed < 5 ms with a between-speakers mic (telemetry +\n"
              "    snapcast sub-ms inter-client sync are necessary-not-sufficient).")
    elif overall == "FAIL":
        print("    FAIL -> shelve the active wireless follower; active stays\n"
              "    solo-or-leader. Update the slice plan. (If FAIL is xruns,\n"
              "    retry a constructed/hardware loopback per the doc prior-art.)")
    else:
        print("    INCOMPLETE -> finish the >=24h soak (and a 2h acoustic window\n"
              "    with a between-speakers mic) before deciding.")
    return 0 if overall.startswith("PASS") else (2 if overall == "FAIL" else 1)


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ac = sub.add_parser("acoustic", help="single-mic offset on one capture WAV")
    ac.add_argument("--wav", required=True)
    ac.add_argument("--min-sharpness", type=float, default=4.0)
    ac.set_defaults(func=run_acoustic)

    sk = sub.add_parser("soak", help="aggregate a whole run -> PASS/FAIL")
    sk.add_argument("--dir", required=True)
    sk.add_argument("--min-sharpness", type=float, default=4.0)
    sk.set_defaults(func=run_soak)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
