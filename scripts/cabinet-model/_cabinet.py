# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Shared model for the cabinet-model tools (see README.md in this folder).

Woofer pair per unit front drive:  P(theta) = A_f(theta) + r(f) * A_r(theta)
  A_i = measured near-field (minimum phase, raw driver) x BEM transfer (10 m far field over the
        near field at the mic spot, 10 m delay removed)
  r   = electrical rear/front ratio of the rear stage
  v_i = cone velocity on A's scale (the BEM sources move at 1 m/s), for cone travel
Seat: a listener in front of the cabinet face at a bearing; the wall behind the cabinet is an
image source with a reflection factor. Phasors are exp(+i w t): a positive delay has negative phase.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from jasper.active_speaker.branch_chain import camilla_filter_response, rear_stage_response
from jasper.audio_measurement.excess_phase import minimum_phase

C = 343.0
FS = 48000
NFFT = 1 << 17


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


class Cabinet:
    """Measured near-field x BEM transfer for both woofers, with a back-wall seat model."""

    def __init__(self, transfer: Path, nearfield: Path, grid: np.ndarray):
        t = np.load(transfer)
        nf = np.load(nearfield)
        self.grid, self.angles = grid, t["angles_deg"]
        self.front_z, self.depth = float(t["front_z_m"]), float(t["depth_m"])
        k = 2 * np.pi * t["f"] / C
        self.A, self.v = {}, {}
        for w in ("front", "rear"):
            trans = np.conj(t[f"far_{w}"] / t[f"nf_{w}"][:, None]) * np.exp(1j * k * t["radius_m"])[:, None]
            near = min_phase(nf["freqs"], nf[f"{w}_raw_db"], grid)
            self.v[w] = near / interp_complex(t["f"], np.conj(t[f"nf_{w}"]), grid)
            self.A[w] = near[:, None] * np.stack([interp_complex(t["f"], trans[:, a], grid)
                                                  for a in range(len(self.angles))], axis=1)

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


def _chain(cfg: dict[str, Any], channel: int, prefix: str, grid: np.ndarray) -> np.ndarray:
    h = np.ones_like(grid, dtype=complex)
    biquads = []
    for step in cfg["pipeline"]:
        if step["type"] != "Filter" or channel not in step["channels"]:
            continue
        for name in step["names"]:
            if not name.startswith(prefix):
                continue
            spec, p = cfg["filters"][name], cfg["filters"][name].get("parameters", {})
            if spec["type"] == "Gain":
                h = h * 10 ** (p["gain"] / 20) * (-1 if p.get("inverted") else 1)
            elif spec["type"] == "Delay":
                h = h * np.exp(-2j * np.pi * grid * p["delay"] / 1000)
            elif spec["type"] in ("Biquad", "BiquadCombo"):
                biquads.append(spec)
    return h * (camilla_filter_response(biquads, grid) if biquads else 1)


def rear_ratio(dsp: Path, grid: np.ndarray, *, front: int = 0, rear: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """(rear/front electrical ratio, front chain) of the rear stage in a CamillaDSP YAML graph
    (the emitted rear_out2_* chains) or in a rear-calibration JSON or prescription document."""
    if dsp.suffix == ".json":
        document = json.loads(dsp.read_text())
        document = document.get("sections", {}).get("rear_calibration", document)
        summed, front_h = rear_stage_response({**document, "rear_muted": False}, grid)
        return summed / front_h, front_h
    cfg = yaml.safe_load(dsp.read_text())
    front_h = _chain(cfg, front, "rear_out2_front", grid)
    rear_h = (_chain(cfg, rear, "rear_out2_bass", grid) + _chain(cfg, rear + 1, "rear_out2_cancellation", grid)) \
        * _chain(cfg, rear, "rear_out2_output", grid)
    return rear_h / front_h, front_h


def seat_deviation(y_db: np.ndarray, grid: np.ndarray, lo: float = 45.0, hi: float = 650.0) -> np.ndarray:
    """A response's deviation from its own 1-octave running mean over lo..hi, holes weighted x2."""
    ly = np.log2(grid)
    near = np.abs(ly[:, None] - ly[None, :]) <= 0.5
    d = y_db - near @ y_db / near.sum(axis=1)
    return np.where(d < 0, 2 * d, d)[(grid >= lo) & (grid <= hi)]


def roughness_db(y_db: np.ndarray, grid: np.ndarray, lo: float = 45.0, hi: float = 650.0) -> float:
    return float(np.sqrt(np.mean(seat_deviation(y_db, grid, lo, hi) ** 2)))
