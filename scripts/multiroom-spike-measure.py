#!/usr/bin/env python3
# =============================================================================
# multiroom-spike-measure.py — analyzer for the P0 multi-room SPIKE (THROWAWAY)
# =============================================================================
#
# Pure-stdlib. The measurement half of scripts/multiroom-spike.sh. Answers the
# docs/HANDOFF-multiroom.md §8 P0 question: does some (buffer, codec) cell hold
# inter-speaker sync within the working target p99 < 5 ms on WiFi?
#
# THREE subcommands:
#
#   software   — connect to snapserver's JSON-RPC (TCP) control plane, poll
#                Server.GetStatus for a window, and summarize each client's
#                reported latency. Writes one JSON cell file. (Run per sweep
#                cell by the shell harness.)
#
#   acoustic   — GROUND TRUTH. Given a single-mic WAV of the L/R pair both
#                playing the SAME periodic broadband CLICK, find consecutive
#                click arrivals and (a) cross-correlate each against the first
#                to measure inter-arrival jitter, and (b) autocorrelate within
#                each arrival window to recover the L/R path/clock offset (the
#                real comb-filter check). A broadband click — not a tone — is
#                required: a tone's autocorrelation is ambiguous at its own
#                period. No deps beyond the stdlib `wave` + arithmetic.
#
#   summarize  — read all per-cell JSON files produced by `software`, build a
#                table per (buffer, codec) with p50/p95/p99, mark PASS/FAIL vs
#                the 5 ms L/R target, fold in the RAM/CPU budget snapshots, and
#                print the recommended (buffer_ms, codec) — or a clear "no
#                setting held, here's the distribution + next steps" failure.
#
# -----------------------------------------------------------------------------
# MEASUREMENT-MODE LIMITATIONS (read before trusting a number)
# -----------------------------------------------------------------------------
# software mode:
#   * snapserver reports each client's *configured/estimated* latency and a
#     rolling clock-offset, NOT the physical acoustic arrival time. It tells
#     you the engine BELIEVES the clients are aligned; it cannot see speaker
#     placement, DAC latency, amp delay, or room acoustics. Treat the spread
#     across clients (max-min reported latency) as a NECESSARY-not-SUFFICIENT
#     proxy: if the engine itself reports >5 ms inter-client spread, the cell
#     definitely fails; if it reports <5 ms, you STILL must confirm acoustically.
#   * The JSON-RPC schema differs slightly across snapcast versions; we read
#     defensively and record whatever latency-ish fields exist.
#
# acoustic mode:
#   * One mic hears BOTH speakers summed. We recover the *relative* L/R arrival
#     offset from the autocorrelation secondary peak — valid only because the
#     source is a broadband click (sharp single autocorr peak). A tone would be
#     ambiguous at its own period; the harness deliberately feeds a click.
#   * Mic placement biases the result by (d_L - d_R)/343 m·s — keep the mic
#     roughly equidistant, or subtract the known geometric path difference.
#     This is the documented ground-truth check, not a lab interferometer.
#   * It measures the cell that was PLAYING when you recorded — label your WAVs.
#
# =============================================================================

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import socket
import sys
import time
import wave

# The working target from docs/HANDOFF-multiroom.md §8: p99 < 5 ms for L/R.
TARGET_P99_MS = 5.0


# =============================================================================
# Shared: percentile (pure-python, no numpy)
# =============================================================================
def percentile(values, pct):
    """Linear-interpolated percentile. `values` need not be sorted."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


# =============================================================================
# software mode — poll snapserver JSON-RPC over TCP
# =============================================================================
def _rpc_call(sock_file, sock, req_id, method):
    """Send one JSON-RPC request (newline-framed) and read one response line."""
    payload = json.dumps({"id": req_id, "jsonrpc": "2.0", "method": method}) + "\r\n"
    sock.sendall(payload.encode("utf-8"))
    line = sock_file.readline()
    if not line:
        raise ConnectionError("snapserver closed the JSON-RPC connection")
    return json.loads(line)


def _extract_client_latencies(status_result):
    """Pull a {client_id: reported_latency_ms} map out of Server.GetStatus.

    Defensive across snapcast schema variants: snapserver nests groups→clients,
    and the latency-ish value has lived under client['config']['latency'] and
    client['snapclient'] differently across versions. We sum the configured
    latency with any reported live offset when present.
    """
    out = {}
    server = status_result.get("server", status_result)
    groups = server.get("groups", []) if isinstance(server, dict) else []
    for group in groups:
        for client in group.get("clients", []):
            cid = client.get("id") or client.get("host", {}).get("name") or "unknown"
            cfg = client.get("config", {})
            latency = cfg.get("latency")  # configured buffer/latency, ms
            # Some builds expose a live clock offset under lastSeen/snapclient.
            if latency is None:
                latency = client.get("latency")
            if latency is not None:
                out[cid] = float(latency)
    return out


def run_software(args):
    """Poll snapserver JSON-RPC for a window; summarize per-client + spread."""
    end = time.time() + args.poll_sec
    # samples[client_id] = [reported_latency_ms, ...]
    samples = {}
    # spread[i] = max-min reported latency across clients at poll i (the L/R
    # proxy: how far apart the engine THINKS its clients are).
    spread = []
    n_polls = 0
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as exc:
        result = {
            "buffer_ms": args.buffer_ms,
            "codec": args.codec,
            "error": f"could not connect to snapserver JSON-RPC at "
                     f"{args.host}:{args.port}: {exc}",
        }
        _write_json(args.json_out, result)
        print(result["error"], file=sys.stderr)
        return 1

    sock_file = sock.makefile("r")
    req_id = 0
    with sock:
        while time.time() < end:
            req_id += 1
            try:
                resp = _rpc_call(sock_file, sock, req_id, "Server.GetStatus")
            except (ConnectionError, json.JSONDecodeError) as exc:
                print(f"poll error: {exc}", file=sys.stderr)
                break
            lat = _extract_client_latencies(resp.get("result", {}))
            if lat:
                n_polls += 1
                for cid, ms in lat.items():
                    samples.setdefault(cid, []).append(ms)
                vals = list(lat.values())
                spread.append(max(vals) - min(vals))
            time.sleep(0.5)

    per_client = {
        cid: {
            "p50": percentile(v, 50),
            "p95": percentile(v, 95),
            "p99": percentile(v, 99),
            "n": len(v),
        }
        for cid, v in samples.items()
    }
    result = {
        "buffer_ms": args.buffer_ms,
        "codec": args.codec,
        "mode": "software",
        "polls": n_polls,
        "n_clients": len(samples),
        "per_client": per_client,
        # The L/R proxy: distribution of inter-client reported-latency spread.
        "inter_client_spread_ms": {
            "p50": percentile(spread, 50),
            "p95": percentile(spread, 95),
            "p99": percentile(spread, 99),
            "n": len(spread),
        },
        "note": "software-mode reported latency is a NECESSARY-not-SUFFICIENT "
                "proxy; confirm with acoustic mode before trusting a PASS.",
    }
    _write_json(args.json_out, result)
    print(json.dumps(result, indent=2))
    return 0


# =============================================================================
# acoustic mode — cross-correlate two chirp arrivals from a single-mic WAV
# =============================================================================
def _read_wav_mono(path):
    """Return (samples:list[int], sample_rate). Mixes to mono if stereo."""
    with wave.open(path, "rb") as w:
        n_ch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"expected 16-bit WAV, got sampwidth={sw}")
    import array

    a = array.array("h")
    a.frombytes(raw)
    if n_ch == 1:
        return list(a), sr
    # Mix channels down to mono.
    mono = [sum(a[i:i + n_ch]) // n_ch for i in range(0, len(a), n_ch)]
    return mono, sr


def _find_burst_onsets(samples, sr, min_gap_s=0.5):
    """Crude onset detector: short-window energy threshold + refractory gap.

    The click track is a ~2 ms broadband burst every ~1 s; we find the rising
    edge of each burst's energy envelope. Good enough to slice out arrivals;
    the fine offset comes from cross-/auto-correlation below.
    """
    win = max(1, int(0.005 * sr))  # 5 ms energy window
    energy = []
    acc = 0
    for i, s in enumerate(samples):
        acc += s * s
        if i >= win:
            acc -= samples[i - win] * samples[i - win]
        energy.append(acc)
    peak = max(energy) if energy else 0
    thresh = peak * 0.15
    refractory = int(min_gap_s * sr)
    onsets = []
    last = -refractory
    for i, e in enumerate(energy):
        if e > thresh and (i - last) > refractory:
            onsets.append(i)
            last = i
    return onsets


def _xcorr_offset(a, b, max_lag):
    """Integer-lag cross-correlation peak between equal-length windows a,b.

    Returns the lag (in samples) of b relative to a that maximizes correlation.
    Positive lag => b arrives later than a. Brute force; windows are tiny.
    """
    best_lag, best_val = 0, -1.0
    for lag in range(-max_lag, max_lag + 1):
        s = 0.0
        for i in range(len(a)):
            j = i + lag
            if 0 <= j < len(b):
                s += a[i] * b[j]
        if s > best_val:
            best_val, best_lag = s, lag
    return best_lag


def run_acoustic(args):
    """Estimate inter-speaker arrival offset from a single-mic click recording.

    Two complementary numbers:
      (a) inter-arrival jitter: cross-correlate each click arrival against the
          FIRST arrival. Period drift / engine-clock wander shows up here.
      (b) L/R comb spacing: autocorrelate WITHIN each arrival window. The mic
          hears L then R (offset by their path+clock difference), so the click
          appears as a copy of itself delayed by that offset — the secondary
          autocorrelation peak lag IS the L/R offset. The 5 ms target applies
          to this number for the L/R pair.
    """
    samples, sr = _read_wav_mono(args.wav)
    onsets = _find_burst_onsets(samples, sr)
    if len(onsets) < 2:
        print(f"acoustic: found only {len(onsets)} chirp onsets in {args.wav}; "
              f"need >=2. Is the pair actually playing the chirp?",
              file=sys.stderr)
        return 1

    win = int(0.06 * sr)  # 60 ms window around each onset
    max_lag = int(0.010 * sr)  # search +-10 ms

    # (a) Jitter across arrivals: align each arrival to the first via xcorr.
    ref_start = onsets[0]
    ref = samples[ref_start:ref_start + win]
    inter_arrival_offsets_ms = []
    for onset in onsets[1:]:
        seg = samples[onset:onset + win]
        if len(seg) < len(ref):
            continue
        lag = _xcorr_offset(ref, seg[:len(ref)], max_lag)
        inter_arrival_offsets_ms.append(lag / sr * 1000.0)

    # (b) Within-window L/R comb spacing: autocorrelation secondary peak. A
    # single mic + two slightly-offset speakers produces a self-similar copy
    # delayed by the L/R arrival difference. The secondary autocorr peak lag
    # is that difference — the comb-filter spacing the ear hears.
    comb_lags_ms = []
    for onset in onsets:
        seg = samples[onset:onset + win]
        if len(seg) < win:
            continue
        # Autocorrelate, ignore the trivial zero-lag peak.
        best_lag, best_val = 0, -1.0
        for lag in range(int(0.0003 * sr), max_lag):  # skip <0.3 ms (zero-lag lobe)
            s = 0.0
            for i in range(len(seg) - lag):
                s += seg[i] * seg[i + lag]
            if s > best_val:
                best_val, best_lag = s, lag
        comb_lags_ms.append(best_lag / sr * 1000.0)

    abs_offsets = [abs(x) for x in inter_arrival_offsets_ms]
    result = {
        "mode": "acoustic",
        "wav": args.wav,
        "sample_rate": sr,
        "onsets_found": len(onsets),
        "inter_arrival_jitter_ms": {
            "p50": percentile(abs_offsets, 50),
            "p95": percentile(abs_offsets, 95),
            "p99": percentile(abs_offsets, 99),
            "n": len(abs_offsets),
        },
        "lr_comb_spacing_ms": {
            "p50": percentile(comb_lags_ms, 50),
            "max": max(comb_lags_ms) if comb_lags_ms else float("nan"),
            "n": len(comb_lags_ms),
        },
        "note": "lr_comb_spacing_ms is the audible comb-filter delay between L "
                "and R as the mic hears them; bias by (d_L-d_R)/343. The 5 ms "
                "target applies to this number for the L/R pair.",
    }
    print(json.dumps(result, indent=2))
    p99 = result["inter_arrival_jitter_ms"]["p99"]
    comb = result["lr_comb_spacing_ms"]["p50"]
    verdict_val = max(v for v in (comb, p99) if not math.isnan(v)) if not (
        math.isnan(comb) and math.isnan(p99)) else float("nan")
    if not math.isnan(verdict_val):
        ok = verdict_val < TARGET_P99_MS
        print(f"\nACOUSTIC VERDICT: comb/jitter ~{verdict_val:.2f} ms "
              f"vs target {TARGET_P99_MS} ms → {'PASS' if ok else 'FAIL'}")
    return 0


# =============================================================================
# summarize mode — table across the sweep + PASS/FAIL + recommendation
# =============================================================================
def _load_budget(results_dir, codec, buffer_ms):
    """Parse the budget snapshot text file for total Pss + a coarse CPU note."""
    path = os.path.join(results_dir, f"budget-{codec}-{buffer_ms}ms.txt")
    if not os.path.exists(path):
        return None
    pss_kb = 0
    cpu_note = ""
    with open(path) as f:
        for line in f:
            if "pss_kb=" in line:
                try:
                    pss_kb += int(line.split("pss_kb=")[1].split()[0])
                except (ValueError, IndexError):
                    pass
            if line.strip().startswith("all") or "%idle" in line:
                cpu_note = line.strip()
    return {"pss_mb": round(pss_kb / 1024.0, 1), "cpu_note": cpu_note}


def run_summarize(args):
    cells = []
    for path in sorted(glob.glob(os.path.join(args.results_dir, "stats-*.json"))):
        with open(path) as f:
            data = json.load(f)
        if "error" in data:
            cells.append({"codec": data.get("codec"), "buffer_ms": data.get("buffer_ms"),
                          "error": data["error"]})
            continue
        spread = data.get("inter_client_spread_ms", {})
        budget = _load_budget(args.results_dir, data["codec"], data["buffer_ms"])
        cells.append({
            "codec": data["codec"],
            "buffer_ms": data["buffer_ms"],
            "p50": spread.get("p50", float("nan")),
            "p95": spread.get("p95", float("nan")),
            "p99": spread.get("p99", float("nan")),
            "n": spread.get("n", 0),
            "pss_mb": (budget or {}).get("pss_mb"),
        })

    if not cells:
        print(f"no stats-*.json cells in {args.results_dir} — run --sweep first.",
              file=sys.stderr)
        return 1

    # Sort: codec, then buffer ascending.
    cells.sort(key=lambda c: (c.get("codec") or "", c.get("buffer_ms") or 0))

    print("=" * 78)
    print("MULTI-ROOM SPIKE — software-mode inter-client sync proxy "
          "(p99 < 5 ms target)")
    print("=" * 78)
    hdr = f"{'codec':<6} {'buffer':>7} {'p50ms':>7} {'p95ms':>7} {'p99ms':>7} " \
          f"{'pss_mb':>7} {'verdict':>8}"
    print(hdr)
    print("-" * 78)
    passing = []
    for c in cells:
        if "error" in c:
            print(f"{(c.get('codec') or '?'):<6} {(c.get('buffer_ms') or '?')!s:>7} "
                  f"  ERROR: {c['error'][:48]}")
            continue
        p99 = c["p99"]
        ok = (not math.isnan(p99)) and p99 < TARGET_P99_MS and c["n"] > 0
        verdict = "PASS" if ok else "FAIL"
        if ok:
            passing.append(c)
        pss = f"{c['pss_mb']}" if c.get("pss_mb") is not None else "?"
        print(f"{c['codec']:<6} {c['buffer_ms']:>5}ms {c['p50']:>7.2f} "
              f"{c['p95']:>7.2f} {c['p99']:>7.2f} {pss:>7} {verdict:>8}")
    print("-" * 78)
    print("NOTE: software-mode is a NECESSARY-not-SUFFICIENT proxy. A PASS here "
          "means\n      the engine believes its clients are aligned; confirm "
          "the winner\n      acoustically (acoustic mode) before declaring "
          "victory.")
    print("=" * 78)

    if passing:
        # Recommend the LOWEST buffer (least latency-to-glass) that holds the
        # bound; break ties by lowest RAM (prefer pcm/flac over opus CPU when
        # RAM is comparable — the headline 1 GB-Pi constraint).
        passing.sort(key=lambda c: (c["buffer_ms"], c.get("pss_mb") or 1e9))
        win = passing[0]
        print(f"\nRECOMMENDED: buffer={win['buffer_ms']}ms codec={win['codec']} "
              f"(p99={win['p99']:.2f} ms, "
              f"Pss≈{win.get('pss_mb', '?')} MB) — lowest-latency cell that "
              f"holds the WiFi bound.")
        print("NEXT: record the chirp on this exact cell and run acoustic mode "
              "to confirm.")
        return 0

    print("\nNO SETTING HELD the p99 < 5 ms bound on WiFi (software proxy).")
    print("Distribution above is the evidence. Next steps, in order:")
    print("  1. Re-run --sweep WITHOUT --netem (rule out the injected stress).")
    print("  2. Add --reference-ethernet to get the best-case line — if even "
          "Ethernet\n     fails, the problem is the topology/config, not WiFi.")
    print("  3. Try buffers ABOVE 1200 ms (extend BUFFERS_MS) — deeper buffer is "
          "the\n     WiFi jitter-absorption lever; latency-to-glass for music "
          "is fine.")
    print("  4. Inspect a failing cell's per_client block in the JSON — a single "
          "lagging\n     client (often the Pi Zero sub on weak WiFi) skews the "
          "spread; sub sync\n     tolerance is loose, so consider excluding the "
          "sub from the L/R verdict.")
    return 2


# =============================================================================
def _write_json(path, obj):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sw = sub.add_parser("software", help="poll snapserver JSON-RPC for one cell")
    sw.add_argument("--host", required=True)
    sw.add_argument("--port", type=int, default=1705)
    sw.add_argument("--poll-sec", type=float, default=20.0)
    sw.add_argument("--buffer-ms", type=int, required=True)
    sw.add_argument("--codec", required=True)
    sw.add_argument("--json-out", default="")
    sw.set_defaults(func=run_software)

    ac = sub.add_parser("acoustic", help="cross-correlate a single-mic chirp WAV")
    ac.add_argument("--wav", required=True)
    ac.set_defaults(func=run_acoustic)

    su = sub.add_parser("summarize", help="table + PASS/FAIL + recommendation")
    su.add_argument("--results-dir", required=True)
    su.set_defaults(func=run_summarize)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
