#!/usr/bin/env python3
"""AEC3 single-variable sweep campaign.

Starts from V2FIXED as baseline. For each knob, sweeps values and
scores against the 4 music cells (the cells where AEC actually matters).
Reports event counts per (config, cell) so we can identify which
knobs improve which cells.

For each cell, the reference numbers:
  whisper-music: raw=1, AEC3=0, V2FIXED=0, D256=2  (wake model ceiling ~1-2)
  fast-music:    raw=8, AEC3=3, V2FIXED=5, D256=6  (opportunity = recover 3)
  yell-music:    raw=11, AEC3=6, V2FIXED=9, D256=7  (opportunity = recover 2)
  normal-music:  raw=5, AEC3=7, V2FIXED=7, D256=9   (already winning)
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, "/tmp/aec3v2-spike")
import importlib, _aec3_v2_spike
importlib.reload(_aec3_v2_spike)
from _aec3_v2_spike import Aec3V2

from openwakeword.model import Model

BASELINE = Path("/Users/jaspercurry/Code/JTS/.claude/worktrees/hardcore-herschel-c614a3/reference-conditions")
MODEL_PATH = "/tmp/jts-wake-models/jarvis_v2.onnx"

# V2FIXED config as the campaign baseline
V2FIXED = dict(
    stream_delay_ms=40,
    ns_enabled=True, ns_level="low",
    agc1_enabled=True, agc1_target_dbfs=9, agc1_max_gain_db=18,
    filter_refined_length_blocks=30,
    ep_strength_bounded_erl=False,        # FIX
    ep_strength_default_gain=0.3,
    erle_max_l=2.0, erle_max_h=1.2, erle_onset_detection=False,
    use_stationarity_properties=True,
    conservative_hf_suppression=True,
    normal_mask_hf_enr_transparent=0.3,
    normal_mask_hf_enr_suppress=0.4,
    normal_mask_hf_emr_transparent=0.3,
    normal_max_dec_factor_lf=0.05,
)

MUSIC_CELLS = ["whisper-music", "fast-music", "yell-music", "normal-music"]
ALL_CELLS = [
    "normal-quiet", "normal-music", "whisper-quiet", "whisper-music",
    "yell-quiet", "yell-music", "fast-quiet", "fast-music",
    "slow-quiet", "slow-music",
]

print("Loading wake model …")
m = Model(wakeword_models=[MODEL_PATH], inference_framework="onnx")
print("ready")


def run_config(opts: dict, cells=MUSIC_CELLS) -> dict:
    """Process the listed cells through Aec3V2(opts) and return event counts + peak scores."""
    out = {}
    for cond in cells:
        with wave.open(str(BASELINE / cond / "aec-off.wav"), "rb") as w:
            mic = w.readframes(w.getnframes())
        with wave.open(str(BASELINE / cond / "reference.wav"), "rb") as w:
            ref = w.readframes(w.getnframes())
        n = (min(len(mic), len(ref)) // 320) * 320
        aec = Aec3V2(opts)
        cleaned = aec.process(mic[:n], ref[:n])
        s = np.frombuffer(cleaned, dtype=np.int16)
        if hasattr(m, "reset"): m.reset()
        scores = np.array([
            float(m.predict(s[i*1280:(i+1)*1280]).get("jarvis_v2", 0.0))
            for i in range(len(s) // 1280)
        ])
        # Event detection: peak crossing with 0.7s refractory
        REFRACTORY = int(0.7 / 0.08)
        events, i = [], 0
        while i < len(scores):
            if scores[i] >= 0.5:
                end = min(i + REFRACTORY, len(scores))
                pi_ = i + int(np.argmax(scores[i:end]))
                events.append((pi_, float(scores[pi_])))
                i = pi_ + REFRACTORY
            else:
                i += 1
        out[cond] = (len(events), float(scores.max()))
    return out


def fmt_row(label: str, results: dict, baseline: dict = None) -> str:
    parts = [f"{label:<38s}"]
    for cell in MUSIC_CELLS:
        n, peak = results.get(cell, (0, 0.0))
        ref_n = baseline.get(cell, (0, 0.0))[0] if baseline else None
        marker = ""
        if ref_n is not None:
            if n > ref_n: marker = "+"
            elif n < ref_n: marker = "-"
            else: marker = " "
        parts.append(f"{n:>3d}/{peak:.2f}{marker}")
    return "  ".join(parts)


# Run baseline first
print("\n=== BASELINE: V2FIXED ===")
t0 = time.time()
baseline = run_config(V2FIXED)
print(fmt_row("baseline", baseline))
print(f"(took {time.time()-t0:.1f}s)\n")

# Single-variable sweeps: each test changes ONE knob from V2FIXED.
# Group by hypothesis.
SWEEPS = [
    # ===== reverts (does V2FIXED's choice actually help?) =====
    ("REVERT: bounded_erl=True (V2FIXED uses False)",
     {"ep_strength_bounded_erl": True}),
    ("REVERT: default_gain=1.0 (V2FIXED uses 0.3)",
     {"ep_strength_default_gain": 1.0}),
    ("REVERT: conservative_hf=False (V2FIXED True)",
     {"conservative_hf_suppression": False}),
    ("REVERT: mask_hf defaults (4x asymmetric)",
     {"normal_mask_hf_enr_transparent": 0.07, "normal_mask_hf_enr_suppress": 0.1}),
    ("REVERT: onset_detection=True (V2FIXED False)",
     {"erle_onset_detection": True}),
    ("REVERT: erle.max_l=4.0, max_h=1.5 (defaults)",
     {"erle_max_l": 4.0, "erle_max_h": 1.5}),
    ("REVERT: stationarity=False (V2FIXED True)",
     {"use_stationarity_properties": False}),
    ("REVERT: max_dec_factor_lf=0.25 (V2FIXED 0.05)",
     {"normal_max_dec_factor_lf": 0.25}),
    ("REVERT: filter length=13 (V2FIXED 30)",
     {"filter_refined_length_blocks": 13}),
    # ===== explore further on V2FIXED knobs =====
    ("MORE: default_gain=0.1 (V2FIXED 0.3, default 1.0)",
     {"ep_strength_default_gain": 0.1}),
    ("MORE: default_gain=0.5",
     {"ep_strength_default_gain": 0.5}),
    ("MORE: erle.max_l=1.5, max_h=1.0 (lower)",
     {"erle_max_l": 1.5, "erle_max_h": 1.0}),
    ("MORE: max_dec_factor_lf=0.02 (V2FIXED 0.05)",
     {"normal_max_dec_factor_lf": 0.02}),
    ("MORE: filter length=50",
     {"filter_refined_length_blocks": 50}),
    ("MORE: filter length=80",
     {"filter_refined_length_blocks": 80}),
    # ===== untouched knobs from the research =====
    ("NEW: nearend mask_hf parity (was 0.1/0.3)",
     {"nearend_mask_hf_enr_transparent": 0.3, "nearend_mask_hf_enr_suppress": 0.4}),
    ("NEW: nearend_average_blocks=16 (default 4)",
     {"nearend_average_blocks": 16}),
    ("NEW: nearend_average_blocks=8",
     {"nearend_average_blocks": 8}),
    ("NEW: echo_can_saturate=False (default True)",
     {"ep_strength_echo_can_saturate": False}),
    ("NEW: audibility_threshold_hf=100 (default 10)",
     {"audibility_threshold_hf": 100.0}),
    ("NEW: dnd_snr_threshold=10 (more aggressive nearend)",
     {"dnd_snr_threshold": 10.0}),
    ("NEW: dnd_hold_duration=100 (V2FIXED 50)",
     {"dnd_hold_duration": 100}),
    ("NEW: nearend max_dec=0.05 (matches normal)",
     {"nearend_max_dec_factor_lf": 0.05}),
    ("NEW: max_inc=1.5 (slower release)",
     {"normal_max_inc_factor": 1.5, "nearend_max_inc_factor": 1.5}),
    # ===== combinations targeted at specific cells =====
    ("COMBO: maxmusic-cancel (whisper-music aim)",
     {"ep_strength_default_gain": 1.0, "use_stationarity_properties": False,
      "filter_refined_length_blocks": 50}),
    ("COMBO: minHF-tear (fast-music aim)",
     {"audibility_threshold_hf": 100.0,
      "nearend_mask_hf_enr_transparent": 0.5,
      "nearend_mask_hf_enr_suppress": 0.6}),
    ("COMBO: anti-pump-strong (yell-music aim)",
     {"erle_max_l": 1.5, "erle_max_h": 1.0,
      "nearend_average_blocks": 16,
      "normal_max_dec_factor_lf": 0.02, "nearend_max_dec_factor_lf": 0.02}),
]

print("\n=== SINGLE-VARIABLE SWEEPS (delta vs V2FIXED) ===")
hdr = f"{'config':<38s}  " + "  ".join(f"{c:>10s}" for c in MUSIC_CELLS)
print(hdr)
print("-" * len(hdr))
print(fmt_row("BASELINE V2FIXED", baseline, baseline))

results_log = {"baseline": baseline}
for label, delta in SWEEPS:
    cfg = dict(V2FIXED)
    cfg.update(delta)
    try:
        result = run_config(cfg)
    except Exception as e:
        print(f"{label:<38s}  ERROR: {e}")
        continue
    results_log[label] = result
    print(fmt_row(label, result, baseline))

print("\nDone.")
