# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A stand-in CamillaDSP binary for the offline emit-loop tests.

Accepts the exact argv the real render invocation uses
(``[binary, --gain=<db>, <config>]``, plus ``--version``), reads the derived
config's ``WavFile`` capture, applies the config's own pipeline, and writes the
``File`` playback output as interleaved little-endian float64 — the same
contract :mod:`jasper.bass_extension.bench.render` expects of the real binary.

**It deliberately imports nothing from ``jasper``.** Its biquad coefficients are
written straight from the RBJ Audio EQ Cookbook here, so a test that renders
through it and grades against
``jasper.active_speaker.linearization_fit.complex_correction_response`` is
comparing two independent implementations rather than one implementation with
itself.

**What it can and cannot prove.** It proves the loop's plumbing, the argv
contract, and that the comparison catches a filter realized at the wrong shape.
It cannot prove that the REAL CamillaDSP realizes an emitted biquad the way the
fit models it — that is the entire question the bench exists to ask, and only
the pinned binary on the Pi answers it. Note also that almost nothing here has
to be faithful: the A/B differences two renders of configs that share every
stage except the linearization, so any error in this file's crossover, mixer,
gain, or delay cancels exactly. Only the linearization biquads are load-bearing.

Unsupported filter or step types exit non-zero rather than passing audio
through untouched, so a test that grows a new stage finds out.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import yaml
from scipy.io import wavfile
from scipy.signal import butter, lfilter, sosfilt

VERSION_LINE = "CamillaDSP 4.1.3"
_SOFT_CLIP_CUBEFACTOR = 1.0 / 6.75

#: Fault injection: realize every shelf at the Q CamillaDSP's ``slope: 6``
#: produces instead of the ``q`` the config asks for.
#:
#: This reproduces the 2026-07-27 defect exactly — the emitted graph says one
#: steepness, the DSP realizes another, and nothing in the fit can see it,
#: because the fit's gate, residual, and prediction all evaluate the shelf the
#: config *claims*. Set to ``1`` in the environment of a render to inject it.
#: The realized Q comes from CamillaDSP's own advanced-shelf derivation
#: (``src/filters/biquad.rs``, RBJ): ``S = slope/12`` and
#: ``Q = 1/sqrt((A + 1/A)·(1/S − 1) + 2)``, which at ``slope: 6`` collapses with
#: the shelf's gain — 0.4759 at −11 dB, against the 0.7071 every evaluator
#: assumes.
SLOPE6_SHELF_ENV = "JTS_FAKE_CAMILLADSP_SLOPE6_SHELVES"


def slope6_shelf_q(gain_db: float) -> float:
    """The Q CamillaDSP's ``slope: 6`` actually realizes for ``gain_db``."""

    amp = 10.0 ** (float(gain_db) / 40.0)
    slope_s = 6.0 / 12.0
    return 1.0 / math.sqrt((amp + 1.0 / amp) * (1.0 / slope_s - 1.0) + 2.0)


class FakeRenderError(RuntimeError):
    """The fake binary refused: an unsupported stage or a malformed config."""


def rbj_biquad(kind: str, f0: float, q: float, gain_db: float, fs: int):
    """RBJ Audio EQ Cookbook normalized ``(b, a)`` for one biquad."""

    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * float(f0) / float(fs)
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * float(q))
    if kind == "Peaking":
        b = [1.0 + alpha * A, -2.0 * cw, 1.0 - alpha * A]
        a = [1.0 + alpha / A, -2.0 * cw, 1.0 - alpha / A]
    elif kind in ("Highshelf", "Lowshelf"):
        beta = 2.0 * math.sqrt(A) * alpha
        sign = 1.0 if kind == "Highshelf" else -1.0
        b = [
            A * ((A + 1.0) + sign * (A - 1.0) * cw + beta),
            -2.0 * sign * A * ((A - 1.0) + sign * (A + 1.0) * cw),
            A * ((A + 1.0) + sign * (A - 1.0) * cw - beta),
        ]
        a = [
            (A + 1.0) - sign * (A - 1.0) * cw + beta,
            2.0 * sign * ((A - 1.0) - sign * (A + 1.0) * cw),
            (A + 1.0) - sign * (A - 1.0) * cw - beta,
        ]
    else:
        raise FakeRenderError(f"unsupported Biquad type {kind!r}")
    a0 = a[0]
    return [x / a0 for x in b], [x / a0 for x in a]


def _apply_filter(signal: np.ndarray, spec: dict, fs: int) -> np.ndarray:
    ftype = spec.get("type")
    params = spec.get("parameters") or {}
    if ftype == "Gain":
        if params.get("mute"):
            return np.zeros_like(signal)
        scale = 10.0 ** (float(params.get("gain", 0.0)) / 20.0)
        if params.get("inverted"):
            scale = -scale
        return signal * scale
    if ftype == "Biquad":
        biquad_type = str(params["type"])
        q = float(params["q"])
        gain = float(params["gain"])
        if os.environ.get(SLOPE6_SHELF_ENV) == "1" and biquad_type in (
            "Highshelf",
            "Lowshelf",
        ):
            q = slope6_shelf_q(gain)
        b, a = rbj_biquad(biquad_type, float(params["freq"]), q, gain, fs)
        return lfilter(b, a, signal)
    if ftype == "BiquadCombo":
        combo = str(params.get("type"))
        order = int(params.get("order", 4))
        if order % 2 or order < 2:
            raise FakeRenderError(f"unsupported LR order {order}")
        if combo == "LinkwitzRileyLowpass":
            btype = "low"
        elif combo == "LinkwitzRileyHighpass":
            btype = "high"
        else:
            raise FakeRenderError(f"unsupported BiquadCombo type {combo!r}")
        # A Linkwitz-Riley of order N is two cascaded Butterworths of N/2.
        sos = butter(order // 2, float(params["freq"]), btype, fs=fs, output="sos")
        return sosfilt(sos, sosfilt(sos, signal))
    if ftype == "Delay":
        unit = str(params.get("unit", "ms"))
        if unit != "ms":
            raise FakeRenderError(f"unsupported Delay unit {unit!r}")
        samples = int(round(float(params.get("delay", 0.0)) * fs / 1000.0))
        if samples <= 0:
            return signal
        return np.concatenate([np.zeros(samples), signal[:-samples]])
    if ftype == "Limiter":
        if not params.get("soft_clip"):
            raise FakeRenderError("hard-clip Limiter is not modelled")
        clip = 10.0 ** (float(params["clip_limit"]) / 20.0)
        scaled = np.clip(signal / clip, -1.5, 1.5)
        return (scaled - _SOFT_CLIP_CUBEFACTOR * scaled**3) * clip
    raise FakeRenderError(f"unsupported filter type {ftype!r}")


def _apply_mixer(channels: list[np.ndarray], mixer: dict) -> list[np.ndarray]:
    geometry = mixer.get("channels") or {}
    out_count = int(geometry["out"])
    length = len(channels[0]) if channels else 0
    out = [np.zeros(length) for _ in range(out_count)]
    for entry in mixer.get("mapping") or ():
        dest = int(entry["dest"])
        for source in entry.get("sources") or ():
            scale = 10.0 ** (float(source.get("gain", 0.0)) / 20.0)
            if source.get("inverted"):
                scale = -scale
            out[dest] = out[dest] + channels[int(source["channel"])] * scale
    return out


def render(config_path: Path, gain_db: float) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    devices = config["devices"]
    fs = int(devices["samplerate"])
    capture, playback = devices["capture"], devices["playback"]
    if capture.get("type") != "WavFile" or playback.get("type") != "File":
        raise FakeRenderError("fake binary renders WavFile capture -> File playback only")
    if playback.get("format") != "F64_LE":
        raise FakeRenderError(f"unsupported playback format {playback.get('format')!r}")

    rate, data = wavfile.read(str(capture["filename"]))
    if rate != fs:
        raise FakeRenderError(f"capture rate {rate} != devices.samplerate {fs}")
    frame = np.atleast_2d(data.T).T.astype(np.float64)
    if np.issubdtype(data.dtype, np.integer):
        frame = frame / float(np.iinfo(data.dtype).max)
    # The main fader precedes every pipeline step (Pipeline::process_chunk).
    frame = frame * (10.0 ** (float(gain_db) / 20.0))
    channels = [np.ascontiguousarray(frame[:, i]) for i in range(frame.shape[1])]

    filters = config.get("filters") or {}
    mixers = config.get("mixers") or {}
    for step in config.get("pipeline") or ():
        if step.get("type") == "Filter":
            for channel in step["channels"]:
                signal = channels[int(channel)]
                for name in step["names"]:
                    signal = _apply_filter(signal, filters[name], fs)
                channels[int(channel)] = signal
        elif step.get("type") == "Mixer":
            channels = _apply_mixer(channels, mixers[step["name"]])
        else:
            raise FakeRenderError(f"unsupported pipeline step {step.get('type')!r}")

    expected = int(playback["channels"])
    if len(channels) != expected:
        raise FakeRenderError(
            f"pipeline produced {len(channels)} channels, playback wants {expected}"
        )
    out = np.stack(channels, axis=1).astype("<f8")
    Path(playback["filename"]).write_bytes(out.tobytes())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(VERSION_LINE)
        return 0
    gain_db = 0.0
    positional: list[str] = []
    for token in args:
        if token.startswith("--gain="):
            gain_db = float(token.split("=", 1)[1])
        elif token.startswith("-"):
            raise FakeRenderError(f"unexpected flag {token!r}")
        else:
            positional.append(token)
    if len(positional) != 1:
        raise FakeRenderError(f"expected exactly one config path, got {positional!r}")
    render(Path(positional[0]), gain_db)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    try:
        raise SystemExit(main())
    except FakeRenderError as exc:
        print(f"fake-camilladsp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
