# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Shared model for the cabinet-model tools (see README.md in this folder).

Woofer pair per unit front drive:  P(theta) = A_f(theta) + r(f) * A_r(theta)
  A_i = measured near-field (minimum phase, raw driver) x BEM transfer (far field at the polar
        radius over the near field at the mic spot, propagation delay removed)
  r   = electrical rear/front ratio of the rear stage
  v_i = cone velocity on A's scale (the BEM sources move at 1 m/s), for cone travel
Seat: a listener in front of the cabinet face at a bearing; the wall behind the cabinet is an
image source with a reflection factor. Phasors are exp(+i w t): a positive delay has negative phase.
at_mic: each woofer alone at the far-field microphone points (bem-transfer.py --mic-m), on the FFT
grid, for the model check against gated far-field takes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from scipy.optimize import least_squares

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.graph_transfer import complex_channel_transfer
from jasper.audio_measurement.deconv import apply_arrival_window, direct_arrival_window
from jasper.audio_measurement.excess_phase import minimum_phase
from jasper.audio_measurement.gating import TAPER_FRACTION, build_gate_window
from jasper.audio_measurement.program_analysis import IR_POST_MS, IR_PRE_MS

C = 343.0
FS = 48000
NFFT = 1 << 17
#: The model's impulse is rolled this far, so what precedes its arrival, wrapped to the buffer's
#: end, comes back inside the analysis's 5 ms window before the peak (IR_PRE_MS).
ROLL_S = 0.010


def db(h: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.abs(h))


def interp_complex(fsrc: np.ndarray, h: np.ndarray, fdst: np.ndarray) -> np.ndarray:
    """Log-frequency interpolation of magnitude and unwrapped phase; below the first point the
    magnitude holds and the group delay stays constant."""
    mag, ph = np.log(np.abs(h)), np.unwrap(np.angle(h))
    lf, ld = np.log(fsrc), np.log(fdst)
    m, p = np.interp(ld, lf, mag), np.interp(ld, lf, ph)
    below = fdst < fsrc[0]
    p[below] = ph[0] + (ph[1] - ph[0]) / (fsrc[1] - fsrc[0]) * (fdst[below] - fsrc[0])
    return np.exp(m + 1j * p)


def min_phase(freqs: np.ndarray, level_db: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Minimum-phase response from a magnitude; outside the measured band a sealed-box
    +12 dB/oct slope below and a steep roll-off above, identical for both woofers."""
    fu = np.fft.rfftfreq(NFFT, 1 / FS)
    lo, hi = freqs[0], freqs[-1]
    d = np.interp(np.log(np.maximum(fu, 1e-3)), np.log(freqs), level_db)
    d[fu < lo] = level_db[0] + 40 * np.log10(np.maximum(fu[fu < lo], 0.5) / lo)
    d[fu > hi] = level_db[-1] - 80 * np.log10(fu[fu > hi] / hi)
    h = 10 ** (d / 20) * np.exp(1j * minimum_phase(d / 20 * np.log(10), NFFT))
    return interp_complex(fu[1:], h[1:], grid)


#: The near-field view's driver id for each woofer the model reads (ADR-0316).
WOOFERS = {"front": "woofer", "rear": "woofer:rear"}
#: The pose kind of a far-field take on each side; a behind pose's distance is from the back panel.
SIDES = {"front": "bearing", "behind": "behind"}


def nearfield_raw(views: Sequence[Path]) -> dict[str, dict[str, Any]]:
    """Each woofer's raw curve at its nearest distance, from `jasper-round-views nearfield`; a later
    view's curve replaces an earlier one's for each woofer it has (a re-run after a fix)."""
    curves = {}
    for view in views:
        drivers = {driver["driver"]: driver for driver in json.loads(view.read_text())["drivers"]}
        for name, driver in WOOFERS.items():
            raw = next((placement["raw"] for placement in drivers.get(driver, {}).get("placements", ())
                        if placement["raw"]), None)
            curves[name] = raw or curves.get(name)
    for name, driver in WOOFERS.items():
        if curves[name] is None:
            raise SystemExit(f"{', '.join(map(str, views))}: no raw near-field curve for the {name} woofer ({driver})")
    return curves


def farfield_takes(view: Mapping[str, Any], mic_m: float) -> dict[tuple[str, str], dict[str, Any]]:
    """Each woofer's take alone in front and behind, from a `jasper-round-views nearfield` view: the
    placement nearest mic_m, its raw curve and the shortest gate of the raw's takes, or what is
    `missing` (no_placement, no_raw, not_gated)."""
    gates = {take["take_id"]: take.get("gate") or {} for take in view["takes"]}
    drivers = {driver["driver"]: driver for driver in view["drivers"]}
    takes: dict[tuple[str, str], dict[str, Any]] = {}
    for side, kind in SIDES.items():
        for woofer, driver in WOOFERS.items():
            placements = [one for one in drivers.get(driver, {}).get("placements", ()) if one.get("kind") == kind]
            if not placements:
                takes[woofer, side] = {"missing": "no_placement"}
                continue
            placement = min(placements, key=lambda one: abs(one["distance_mm"] / 1000 - mic_m))
            raw, take = placement["raw"], {"missing": None, "distance_m": placement["distance_mm"] / 1000,
                                           "take_ids": placement["take_ids"]}
            raw_gates = [gates.get(take_id, {}) for take_id in (raw or {}).get("take_ids", ())]
            if raw is None:
                take["missing"] = "no_raw"
            elif not all(gate.get("window") == "gated" for gate in raw_gates):
                take["missing"] = "not_gated"
            else:
                take.update(raw=raw, gate=min(raw_gates, key=lambda gate: gate["window_ms"]),
                            high_hz=placement["trusted_band"].get("high_hz"))
            takes[woofer, side] = take
    return takes


def gated_db(h: np.ndarray, window_ms: float, freqs: np.ndarray) -> np.ndarray:
    """dB at freqs of a response on the FFT grid, read as the analysis reads a gated take: its
    arrival window around the peak, then the gate's window of window_ms."""
    ir = np.roll(np.fft.irfft(h, NFFT), round(ROLL_S * FS))
    segment = apply_arrival_window(ir, direct_arrival_window(ir, FS, pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS))
    gate = build_gate_window(segment.size, peak_idx=int(np.argmax(np.abs(segment))), span=round(window_ms * FS / 1000),
                             taper_fraction=TAPER_FRACTION)
    return db(np.interp(freqs, np.fft.rfftfreq(NFFT, 1 / FS), np.abs(np.fft.rfft(segment * gate, NFFT))))


class Cabinet:
    """Measured near-field x BEM transfer for both woofers, with a back-wall seat model."""

    def __init__(self, transfer: Path, nearfield: Sequence[Path], grid: np.ndarray):
        t = np.load(transfer)
        nf = nearfield_raw(nearfield)
        self.nearfield_take_ids = {WOOFERS[w]: raw["take_ids"] for w, raw in nf.items()}
        self.grid, self.angles = grid, t["angles_deg"]
        self.front_z, self.depth, self.radius = float(t["front_z_m"]), float(t["depth_m"]), float(t["radius_m"])
        self.solved_to_hz = float(t["f"][-1])
        self.mic_m = float(t["mic_m"]) if "mic_m" in t else None
        k = 2 * np.pi * t["f"] / C
        below = grid < t["f"][0]
        fft = np.fft.rfftfreq(NFFT, 1 / FS)[1:]
        self.A, self.v, self.at_mic = {}, {}, {}
        for w in ("front", "rear"):
            curve = (np.asarray(nf[w]["freqs_hz"], float), np.asarray(nf[w]["level_db"], float))
            trans = np.conj(t[f"far_{w}"] / t[f"nf_{w}"][:, None]) * np.exp(1j * k * self.radius)[:, None]
            near = min_phase(*curve, grid)
            p_nf = interp_complex(t["f"], np.conj(t[f"nf_{w}"]), grid)
            p_nf[below] *= grid[below] / t["f"][0]  # near-field pressure per m/s rises with f below the solve
            self.v[w] = near / p_nf
            self.A[w] = near[:, None] * np.stack([interp_complex(t["f"], trans[:, a], grid)
                                                  for a in range(len(self.angles))], axis=1)
            if self.mic_m is not None:
                near_fft = min_phase(*curve, fft)
                for side in SIDES:
                    # The delay over mic_m comes off, as the polar's over its radius does.
                    at = np.conj(t[f"mic_{side}_{w}"] / t[f"nf_{w}"]) * np.exp(1j * k * self.mic_m)
                    self.at_mic[w, side] = np.concatenate(([0.0], near_fft * interp_complex(t["f"], at, fft)))

    def index(self, deg: float) -> int:
        return int(np.argmin(np.abs(((self.angles - deg) + 180) % 360 - 180)))

    def at_angle(self, r: np.ndarray, deg: float) -> np.ndarray:
        a = self.index(deg)
        return self.A["front"][:, a] + r * self.A["rear"][:, a]

    def seat(self, r: np.ndarray, deg: float = 0.0, *, listener_m: float = 2.0, wall_gap_m: float = 0.2,
             reflection: float = 0.9) -> np.ndarray:
        """Pressure listener_m in front of the cabinet face at a bearing, wall_gap_m behind its back."""
        k = 2 * np.pi * self.grid / C
        wall_z = self.front_z - self.depth - wall_gap_m
        img_z = 2 * wall_z - self.front_z
        lx, lz = listener_m * np.sin(np.radians(deg)), self.front_z + listener_m * np.cos(np.radians(deg))
        l_img = np.hypot(lx, lz - img_z)
        mirrored = self.at_angle(r, 180.0 - np.degrees(np.arctan2(lx, lz - img_z)))
        return self.at_angle(r, deg) + reflection * mirrored * (listener_m / l_img) * np.exp(-1j * k * (l_img - listener_m))


def rear_ratio(dsp: Path, grid: np.ndarray, *, front: int = 0, rear: int = 2) -> np.ndarray:
    """Rear/front electrical ratio of a CamillaDSP YAML graph (woofer outputs `front` and `rear`,
    program centre on both inputs, dynamic bass at rest) or of the rear stage in a rear-calibration
    JSON or prescription document (modelled unmuted: the design, not its on/off switch)."""
    if dsp.suffix == ".json":
        document = json.loads(dsp.read_text())
        if "sections" in document:
            if "rear_calibration" not in document["sections"]:
                raise SystemExit(f"{dsp}: the prescription has no rear_calibration section")
            document = document["sections"]["rear_calibration"]
        summed, front_h = rear_stage_response({**document, "rear_muted": False}, grid)
        return summed / front_h
    out = complex_channel_transfer(yaml.safe_load(dsp.read_text()), grid, input_weights={0: 1.0, 1: 1.0},
                                   output_channels={"front": front, "rear": rear},
                                   allow_limiter_passthrough=True, dynamic_bass_at_rest=True)
    return out["rear"] / out["front"]


def sealed_fit(freqs: np.ndarray, y_db: np.ndarray, lo: float, hi: float) -> tuple[float, float, float]:
    """2nd-order high-pass fit of a level curve over lo..hi Hz: (corner Hz, Q, rms dB)."""
    sel = (freqs >= lo) & (freqs <= hi)

    def model(p):
        s = 1j * freqs[sel] / p[1]
        return p[0] + 20 * np.log10(np.abs(s * s / (s * s + s / p[2] + 1)))

    fit = least_squares(lambda p: model(p) - y_db[sel], x0=(np.median(y_db[sel]), 70.0, 0.7),
                        bounds=((-300, 20, 0.3), (300, 200, 3.0)))
    return float(fit.x[1]), float(fit.x[2]), float(np.sqrt(np.mean(fit.fun ** 2)))


def seat_deviation(y_db: np.ndarray, grid: np.ndarray, lo: float = 45.0, hi: float = 650.0) -> np.ndarray:
    """A response's deviation from its own 1-octave running mean over lo..hi, holes weighted x2."""
    ly = np.log2(grid)
    near = np.abs(ly[:, None] - ly[None, :]) <= 0.5
    d = y_db - near @ y_db / near.sum(axis=1)
    return np.where(d < 0, 2 * d, d)[(grid >= lo) & (grid <= hi)]


def roughness_db(y_db: np.ndarray, grid: np.ndarray, lo: float = 45.0, hi: float = 650.0) -> float:
    return float(np.sqrt(np.mean(seat_deviation(y_db, grid, lo, hi) ** 2)))
