#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Near-field -> far-field transfer of each woofer from a solved Boundary Lab case.

Reads a completed case of the CAD repo's Boundary Lab study (read-only), integrates the solver's
own surface solution (Kirchhoff-Helmholtz) to the microphone spots on each woofer's axis, and
saves the pressure there and on the case's horizontal polar. The case's woofers face +z (front)
and -z (rear), and its polar origin sits on the cabinet's front face (the seat model's
reference). Two gates run first:
  1. the integral must reproduce the solver's own polar probes (relative error < 1e-3);
  2. with --measured-step, the model's gap -> gap+step level change must match the measured
     one within 0.2 dB (warns otherwise).

    .venv/bin/python scripts/cabinet-model/bem-transfer.py \\
        --case "$CAD/build/workbench/boundary_lab_mac/system-24mm-compound" --out transfer.npz \\
        --measured-step front=-2.37 --measured-step rear=-2.27

Boundary Lab 0.4.2 stores exp(-i w t) phasors; the cabinet tools conjugate on read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

C = 343.0


def _subdivision(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Barycentric centroids and equal weights of the n*n sub-triangles of a triangle."""
    pts = []
    for i in range(n):
        for j in range(n - i):
            a = np.array([i, j], dtype=float)
            pts.append((a + 1 / 3) / n)
            if i + j < n - 1:
                pts.append((a + 2 / 3) / n)
    bc = np.array(pts)
    return np.column_stack((1 - bc.sum(axis=1), bc[:, 0], bc[:, 1])), np.full(len(bc), 1.0 / len(bc))


FAR_RULE, NEAR_RULE = _subdivision(3), _subdivision(24)


def kh(x, k, xyz, area, normal, size, p_nodes, q_tri):
    """Pressure at x from nodal pressure (e, t, 3) and per-triangle dp/dn (e, t); the mesh
    normal points into the fluid. Triangles within 3 edge lengths get a 24x24 subdivision."""
    out = np.zeros(p_nodes.shape[0], dtype=complex)
    near = np.linalg.norm(xyz.mean(axis=1) - x, axis=1) < 3 * size
    for mask, (lam, w) in ((~near, FAR_RULE), (near, NEAR_RULE)):
        if mask.any():
            y = np.einsum("qv,tvd->tqd", lam, xyz[mask])
            d = y - x
            r = np.linalg.norm(d, axis=2)
            g = np.exp(1j * k * r) / (4 * np.pi * r)
            dgdn = g * (1j * k - 1 / r) * np.einsum("tqd,td->tq", d, normal[mask]) / r
            wa = w[None, :] * area[mask][:, None]
            out += np.einsum("etq,tq->e", np.einsum("qv,etv->etq", lam, p_nodes[:, mask]) * dgdn[None], wa)
            out -= np.einsum("et,tq->e", q_tri[:, mask], g * wa)
    return out


def load(case: Path, run: str, max_hz: float):
    meta = json.loads((case / run / "domains.json").read_text())
    dom = next(d for d in meta["domains"] if d["id"] == "domain:bem-boundary")
    with np.load(case / run / "domains.npz", allow_pickle=False) as a:
        pts = a[dom["coordinates"]["points_m"]]
        tri = a[dom["topology"]["triangles"]]
        tags = a[dom["topology"]["source_physical_tag"]]
    rows = []
    for item in json.loads((case / run / "manifest.json").read_text())["results"]:
        m = json.loads((case / run / item["metadata_file"]).read_text())
        if m["freq_hz"] <= max_hz:
            with np.load(case / run / item["arrays_file"]) as arr:
                rows.append({"f": m["freq_hz"], "exc": m["excitation_port_ids"],
                             **{x["id"]: arr[x["key"]].astype(complex) for x in m["quantities"]}})
    return pts, tri, tags, sorted(rows, key=lambda r: r["f"])


def measured_step(text: str) -> tuple[str, float]:
    woofer, _, value = text.partition("=")
    if woofer not in ("front", "rear"):
        raise argparse.ArgumentTypeError(f"{text!r}: expected front=DB or rear=DB")
    return woofer, float(value)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", type=Path, required=True, help="solved Boundary Lab case folder")
    ap.add_argument("--run", default="full-q4")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gap-m", type=float, default=0.01456,
                    help="mic tip to dust-cap apex; the E150HE-44 surround top sits 14.56 mm above the apex")
    ap.add_argument("--step-m", type=float, default=0.015, help="second spot this much farther out")
    ap.add_argument("--max-hz", type=float, default=1000.0)
    ap.add_argument("--measured-step", action="append", default=[], type=measured_step, metavar="WOOFER=DB",
                    help="the near-field view's 15 -> 30 mm step_db for that woofer, e.g. front=-2.37")
    args = ap.parse_args()

    pts, tri, tags, rows = load(args.case, args.run, args.max_hz)
    project = json.loads((args.case / "trial.blab.json").read_text())["physical_system"]
    groups = {b["id"]: b["group"]["tag"] for b in project["boundaries"]}
    comps = {c["id"].split(":")[1]: c["boundary_ids"] for c in project["components"]}
    xyz = pts[tri]
    cross = np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    normal = cross / (2 * area[:, None])
    size = np.max(np.linalg.norm(xyz - np.roll(xyz, 1, axis=1), axis=2), axis=1)
    obs = json.loads((args.case / "observations.json").read_text())
    origin, radius = np.array(obs["origin_m"]), obs["radius_m"]
    ang = np.deg2rad(np.array(obs["angles_deg"]))
    probes = origin + radius * np.column_stack((np.sin(ang), np.zeros_like(ang), np.cos(ang)))
    depth = json.loads((args.case.parent / "source-facts.json").read_text())["cabinet"]["depth"] / 1000

    def apex(name: str, sign: float) -> np.ndarray:
        """Axial tip of a woofer's dust cap: on the axis through its source's centre (the woofers face
        +z and -z), the source node nearest that axis, outermost along z."""
        nodes = pts[np.unique(tri[np.isin(tags, [groups[b] for b in comps[name]])])]
        centre = (nodes[:, :2].min(axis=0) + nodes[:, :2].max(axis=0)) / 2
        off_axis = np.hypot(*(nodes[:, :2] - centre).T)
        axial = nodes[off_axis <= off_axis.min() + 1e-6]
        return np.array([*centre, axial[:, 2].max() if sign > 0 else axial[:, 2].min()])

    spots = {"front": (apex("front", 1), 1.0), "rear": (apex("rear", -1), -1.0)}
    exc_index = {e.split(":")[1]: i for i, e in enumerate(rows[0]["exc"])}
    errors = []
    out = {k: [] for k in ("f", "nf_front", "nf_rear", "nf2_front", "nf2_rear", "far_front", "far_rear")}
    for row in rows:
        k = 2 * np.pi * row["f"] / C
        pn, qn = row["acoustic:pressure:bem-boundary"][:, tri], row["acoustic:normal-derivative:bem-boundary"]
        for idx in (0, len(ang) // 2):
            ref = row["acoustic:pressure:probe:horizontal"][:, idx]
            got = kh(probes[idx], k, xyz, area, normal, size, pn, qn)
            errors.append(np.max(np.abs(got - ref) / np.abs(ref)))
        out["f"].append(row["f"])
        for w, (tip, sign) in spots.items():
            e = exc_index[w]
            near = [kh(tip + np.array([0, 0, sign * g]), k, xyz, area, normal, size, pn, qn)[e]
                    for g in (args.gap_m, args.gap_m + args.step_m)]
            out[f"nf_{w}"].append(near[0])
            out[f"nf2_{w}"].append(near[1])
            out[f"far_{w}"].append(row["acoustic:pressure:probe:horizontal"][e])
    worst = float(np.max(errors))
    print(f"gate 1: surface integral vs solver probes, max relative error {worst:.1e}")
    if not worst <= 1e-3:
        print("FAIL: the integral does not reproduce the solver; check the case and its normals", file=sys.stderr)
        return 1
    measured = dict(args.measured_step)
    f = np.array(out["f"])
    band = (f >= 35) & (f <= 400)
    for w in ("front", "rear"):
        step = 20 * np.log10(np.abs(np.array(out[f"nf2_{w}"]) / np.array(out[f"nf_{w}"])))
        line = f"gate 2: {w} {args.gap_m*1000:.1f} -> {(args.gap_m+args.step_m)*1000:.1f} mm model {np.mean(step[band]):+.2f} dB"
        if w in measured:
            miss = abs(np.mean(step[band]) - measured[w])
            line += f", measured {measured[w]:+.2f} dB ({'ok' if miss <= 0.2 else 'MISS > 0.2 dB'})"
        print(line)
    with open(args.out, "wb") as fh:
        np.savez(fh, radius_m=radius, angles_deg=np.array(obs["angles_deg"]), front_z_m=origin[2], depth_m=depth,
                 **{k: np.array(v) for k, v in out.items() if not k.startswith("nf2_")})
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
