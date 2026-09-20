"""Shared helpers for the BA before/after job (#5405 follow-up)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent
POSE0 = "az+0.00_el+0.00_d+1.00"
POSE_P20 = "az+20.00_el+0.00_d+1.00"
POSE_M20 = "az-20.00_el+0.00_d+1.00"
POSES3 = (POSE0, POSE_P20, POSE_M20)


def pose_key(az):
    return f"az{az:+06.2f}_el+0.00_d+1.00"


#: Round BA3's check bearings: the room fit never sees these.
CHECK_POSES = tuple(pose_key(a) for a in (-30.0, -10.0, 10.0, 30.0))


def load(path):
    return json.loads(Path(path).read_text())


def curve(result, mic, pose, fp8):
    """(freqs, magnitude_db) of the candidate whose fingerprint starts fp8."""
    if pose not in result["by_pose"][mic]:
        raise SystemExit(f"{pose} is not in this round at {mic}")
    table = result["by_pose"][mic][pose]["candidates"]
    match = [k for k in table if k.startswith(fp8)]
    if len(match) != 1:
        raise SystemExit(f"{fp8} matched {len(match)} candidates at {mic}/{pose}")
    row = table[match[0]]
    return np.asarray(row["freqs_hz"], float), np.asarray(row["magnitude_db"], float)


def smooth(freqs, mags, octaves):
    """Fractional-octave smoothing on a log-frequency grid (+-octaves/2)."""
    out = np.empty_like(mags)
    logf = np.log2(freqs)
    half = octaves / 2.0
    for i, lf in enumerate(logf):
        sel = (logf >= lf - half) & (logf <= lf + half)
        out[i] = float(np.mean(mags[sel]))
    return out


def third_octave_centres(lo, hi):
    centres = 1000.0 * 2.0 ** (np.arange(-20, 14) / 3.0)
    return centres[(centres >= lo) & (centres <= hi)]


def band_mean(freqs, mags, centre, octaves=1 / 3):
    lo, hi = centre * 2 ** (-octaves / 2), centre * 2 ** (octaves / 2)
    sel = (freqs >= lo) & (freqs <= hi)
    if not sel.any():
        return float(np.interp(centre, freqs, mags))
    return float(np.mean(mags[sel]))


def filter_response_db(filters, freqs):
    from jasper.active_speaker.branch_chain import camilla_filter_response
    if not filters:
        return np.zeros(len(freqs))
    return 20.0 * np.log10(np.abs(camilla_filter_response(filters, np.asarray(freqs, float))))


def peaking(freq, gain, q):
    return {"type": "Biquad", "parameters": {"type": "Peaking", "freq": float(freq),
                                             "gain": float(gain), "q": float(q)}}


def shelf(kind, freq, gain, q=0.7071):
    return {"type": "Biquad", "parameters": {"type": kind, "freq": float(freq),
                                             "gain": float(gain), "q": float(q)}}
