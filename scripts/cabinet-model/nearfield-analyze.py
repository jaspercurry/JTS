#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Each woofer's raw near-field response from pair takes pulled off the speaker.

Pull each run's session folder off the speaker (README.md has the command), then name the folders,
each optionally with the attempts to use. Take each spacing as its own run; --compare prints the
level step that bem-transfer.py's gate 2 checks:

    .venv/bin/python scripts/cabinet-model/nearfield-analyze.py nf_front:1-4 nf_rear --out nearfield.npz
    .venv/bin/python scripts/cabinet-model/nearfield-analyze.py nf_front30 nf_rear30 --out nf30.npz \\
        --compare nearfield.npz

A take's woofer is the louder of its two solo sweeps. Every sweep is located on its own (the
playback timeline can jump between segments at near-field levels), drift-corrected with the take's
fitted clock and deconvolved against the take's own stimulus. The fader, the played path (the
repo's graph walker) and a plain bass-extension Loudness boost are divided out, which leaves each
raw driver on one digital reference.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

from _cabinet import sealed_fit
from jasper.active_speaker.graph_transfer import complex_channel_transfer
from jasper.audio_measurement.program import ExcitationProgram, segment_stimulus
from jasper.bass_extension.dynamic import DynamicBassDescriptor, expected_boost_db

FS = 48000
NFFT = 1 << 19
FREQS = np.fft.rfftfreq(NFFT, 1 / FS)
PRE, POST = 2400, int(0.45 * FS)
SEARCH = 12000  # samples each side of the clock-fit prediction (250 ms)
WINDOW = np.ones(int(0.30 * FS) + 240)  # 5 ms before the impulse peak to 300 ms after
WINDOW[:96] = 0.5 * (1 - np.cos(np.linspace(0, np.pi, 96)))
WINDOW[-4800:] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, 4800)))
SWEEPS = {"front": ("sweep_w", "sweep_w_rep"), "rear": ("sweep_t", "sweep_t_rep")}
PLAIN_BASS = {f"bass_ext_dynamic_{n}" for n in ("volume_ramp", "loudness", "delta_highpass", "detector_lowpass")}
BANDS = ((25, 35), (35, 50), (50, 70), (70, 100), (100, 200), (200, 400), (400, 700), (700, 1000))
STEP_BAND_HZ = (35, 400)


def attempts(spec: str) -> tuple[Path, set[int] | None]:
    folder, _, sel = spec.partition(":")
    if not sel:
        return Path(folder), None
    chosen: set[int] = set()
    for part in sel.split(","):
        lo, _, hi = part.partition("-")
        chosen.update(range(int(lo), int(hi or lo) + 1))
    return Path(folder), chosen


def sweep_spectrum(rec: np.ndarray, take: dict, segment) -> np.ndarray:
    fit = take["branch_diagnostic"]
    eps = fit["clock_epsilon_ppm"] * 1e-6
    n, ref = segment.n_samples, np.asarray(segment_stimulus(segment), float)
    a = int(fit["global_offset_samples"] + segment.start_sample * (1 + eps)) - SEARCH
    xc = np.abs(fftconvolve(rec[a:a + n + 2 * SEARCH], ref[::-1], mode="valid"))
    k = int(np.argmax(xc))
    if not 0 < k < xc.size - 1:
        raise SystemExit(f"{take['take_id']}: {segment.segment_id} is not within {SEARCH / FS * 1000:.0f} ms of the clock fit")
    y0, y1, y2 = xc[k - 1:k + 2]
    r0 = a + k + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) - PRE * (1 + eps)
    i0 = int(np.floor(r0))
    raw = rec[i0:i0 + int((n + PRE + POST) * (1 + eps)) + 3]
    seg = np.interp(r0 - i0 + np.arange(n + PRE + POST) * (1 + eps), np.arange(raw.size), raw)
    stim = np.zeros(n + PRE + POST)
    stim[PRE:PRE + n] = ref
    R, S = np.fft.rfft(seg, NFFT), np.fft.rfft(stim, NFFT)
    floor = 1e-6 * np.max(np.abs(S[(FREQS >= 20) & (FREQS <= 20000)]) ** 2)
    ir = np.roll(np.fft.irfft(R * np.conj(S) / (np.abs(S) ** 2 + floor), NFFT), PRE)
    pk = int(np.argmax(np.abs(ir[:PRE + 9600])))
    w = np.zeros(NFFT)
    w[pk - 240:pk - 240 + WINDOW.size] = WINDOW
    return np.fft.rfft(np.roll(ir * w, -PRE), NFFT)


def played_db(take: dict, input_channel: int, freqs: np.ndarray) -> np.ndarray:
    """Level of the drive that played the take's woofer: fader, played path and a plain Loudness boost."""
    cfg = take["provenance"]["graph"]["config"]
    F = cfg["filters"]
    shaped = sorted(n for n in F if n.startswith("bass_ext_dynamic_") and n not in PLAIN_BASS)
    if shaped:
        raise SystemExit(f"{take['take_id']}: the played bass boost is shaped ({shaped[0]}...), which this tool "
                         "does not model; capture with the bass layer cleared (nearfield-plan.py does)")
    outputs = complex_channel_transfer(cfg, freqs, input_weights={input_channel: 1.0},
                                       output_channels={ch: ch for ch in range(cfg["devices"]["playback"]["channels"])},
                                       allow_limiter_passthrough=True, dynamic_bass_at_rest=True)
    path = max(outputs.values(), key=lambda h: float(np.sum(np.abs(h) ** 2)))
    out = take["level_db"] + 20 * np.log10(np.abs(path))
    if "bass_ext_dynamic_loudness" in F:
        p, hp = F["bass_ext_dynamic_loudness"]["parameters"], F.get("bass_ext_dynamic_delta_highpass")
        descriptor = DynamicBassDescriptor(
            low_boost_db=p["low_boost"], reference_level_db=p["reference_level"],
            detector_lowpass_hz=F["bass_ext_dynamic_detector_lowpass"]["parameters"]["freq"],
            compressor_threshold_dbfs=next(iter(cfg["processors"].values()))["parameters"]["threshold"],
            delta_highpass_hz=hp and hp["parameters"]["freq"])
        out = out + np.array(expected_boost_db(descriptor, take["loudness_volume_db"], freqs))
    return out


def per_band(values: np.ndarray, freqs: np.ndarray) -> str:
    return "".join(f"{np.mean(values[(freqs >= lo) & (freqs < hi)]):9.2f}" for lo, hi in BANDS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="pulled session folder[:attempts], e.g. nf_front:1-4")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--compare", type=Path, help="another output of this tool: prints the level step per band")
    args = ap.parse_args()
    keep = (FREQS >= 10) & (FREQS <= 5000)
    fq = FREQS[keep]
    header = "".join(f"{f'{lo}-{hi}':>9s}" for lo, hi in BANDS)
    power: dict[str, list[np.ndarray]] = {"front": [], "rear": []}
    print(f"{'take':18s} woofer  other  fader  SPL max | one-sweep SNR per band, dB\n{'':50s}{header}")
    for spec in args.folders:
        folder, chosen = attempts(spec)
        used = 0
        for path in sorted(glob.glob(str(folder / "evidence/v1/artifacts/crossover_v2/*/positions/*take_*.json"))):
            take = json.loads(Path(path).read_text())
            if chosen is not None and take["attempt"] not in chosen:
                continue
            used += 1
            segments = {s.segment_id: s for s in ExcitationProgram.from_dict(take["program"]).segments}
            rec = wavfile.read(folder / take["wav_path"])[1].astype(float) / 2 ** 31
            spectra = {w: [sweep_spectrum(rec, take, segments[sid])[keep] for sid in sids] for w, sids in SWEEPS.items()}
            mid = (fq >= 100) & (fq <= 500)
            level = {w: np.mean(np.abs(a[mid]) ** 2 + np.abs(b[mid]) ** 2) for w, (a, b) in spectra.items()}
            woofer = max(level, key=level.get)
            a, b = spectra[woofer]
            drive = played_db(take, segments[SWEEPS[woofer][0]].channel, fq)
            power[woofer].append(0.5 * (np.abs(a) ** 2 + np.abs(b) ** 2) / 10 ** (drive / 10))
            # From the repeat's level difference, not its phase: the coarse locate may pick either
            # of two near-equal correlation lobes ~2.6 ms apart, which moves the phase only.
            d = 20 * np.log10(np.abs(a) / np.abs(b))
            snr = [20 * np.log10(20 / np.log(10) / np.sqrt(np.mean(d[s] ** 2)))
                   for s in ((fq >= lo) & (fq < hi) for lo, hi in BANDS)]
            other = 10 * np.log10(min(level.values()) / max(level.values()))
            spl = take["capture_integrity"]["spl"].get("max_window_db_spl") or float("nan")
            print(f"{folder.name + ':' + str(take['attempt']):18s} {woofer:6s} {other:6.1f} {take['level_db']:6.1f} "
                  f"{spl:8.1f} |" + "".join(f"{v:9.1f}" for v in snr))
        if not used:
            raise SystemExit(f"{spec}: no takes found")
    result = {"freqs": fq}
    print(f"\n{'':32s}{header}")
    for woofer, rows in power.items():
        if rows:
            mean = np.mean(rows, axis=0)
            result[f"{woofer}_raw_db"] = 10 * np.log10(mean)
            fc, q, rms = sealed_fit(fq, result[f"{woofer}_raw_db"], 25, 300)
            spread = np.ptp([10 * np.log10(r / mean) for r in rows], axis=0)
            print(f"{f'{woofer}: {len(rows)} take(s), spread':32s}{per_band(spread, fq) if len(rows) > 1 else '':72s}"
                  f"   sealed fit fc {fc:.1f} Hz, Qtc {q:.2f} (rms {rms:.2f} dB)")
    if len(result) == 3:
        print(f"{'rear minus front, dB':32s}{per_band(result['rear_raw_db'] - result['front_raw_db'], fq)}")
    if args.compare:
        other_run = np.load(args.compare)
        step_band = (fq >= STEP_BAND_HZ[0]) & (fq <= STEP_BAND_HZ[1])
        for key in sorted(set(result) & set(other_run) - {"freqs"}):
            step = result[key] - np.interp(fq, other_run["freqs"], other_run[key])
            woofer = key.removesuffix("_raw_db")
            print(f"{woofer + ' step vs ' + args.compare.name:32s}{per_band(step, fq)}"
                  f"   {STEP_BAND_HZ[0]}-{STEP_BAND_HZ[1]} Hz: --measured-step {woofer}={np.mean(step[step_band]):.2f}")
    with open(args.out, "wb") as fh:
        np.savez(fh, **result)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
