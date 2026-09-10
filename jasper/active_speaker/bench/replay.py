# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Replay an exact graph through native DSP without opening audio devices."""

from __future__ import annotations

from dataclasses import asdict
from collections.abc import Mapping
from pathlib import Path
import math
import wave

import numpy as np

from jasper.audio_measurement.bundles import sha256_file
from jasper.audio_measurement.deconv import DEFAULT_MAX_CAPTURE_SECONDS
from jasper.audio_measurement.snr_policy import band_levels_dbfs

from ..measurement_bass import BASS_BANDS_HZ

from .derivation import ArtifactHeader, derive_offline_render_config
from .render import DEPLOYED_PROCESSING_PRECISION, RenderBounds, render_config, resolve_render_binary


def replay_graph(graph: Path, stimulus: Path, out: Path, *, main_db: float, bass_reference_db: float) -> dict:
    if any(not math.isfinite(value) or not -120 <= value <= 0 for value in (main_db, bass_reference_db)):
        raise ValueError("dsp_replay_fader_invalid")
    with wave.open(str(stimulus), "rb") as wav:
        header = ArtifactHeader(wav.getframerate(), wav.getnchannels(), 8 * wav.getsampwidth())
        duration_s = wav.getnframes() / wav.getframerate()
    out.mkdir(parents=True, exist_ok=True)
    raw, config = out.resolve() / "output.f64le", out.resolve() / "render.yml"
    derived = derive_offline_render_config(graph.read_text(), roles=None,
        capture_filename=str(stimulus.resolve()), capture_header=header,
        playback_filename=str(raw), processing_precision=DEPLOYED_PROCESSING_PRECISION)
    config.write_text(derived.yaml_text)
    binary = resolve_render_binary()
    invocation = render_config(binary.path, config, output_path=raw,
        bounds=RenderBounds(timeout_s=max(30, duration_s * 2), rlimit_as_bytes=384 * 1024**2,
                            rlimit_cpu_s=max(30, math.ceil(duration_s * 2)), nice=10),
        fader_db=main_db, loudness_fader_db=bass_reference_db)
    return {"schema": "jts_dsp_replay/1", "graph": str(graph), "graph_sha256": sha256_file(graph),
            "stimulus": str(stimulus), "stimulus_sha256": sha256_file(stimulus),
            "main_db": main_db, "bass_reference_db": bass_reference_db,
            "sample_rate_hz": derived.sample_rate_hz, "channels": derived.playback_channels,
            "output": str(raw), "binary": binary.identity_artifact(), "render": asdict(invocation),
            "derivation": derived.receipt,
            "scope": "Digital graph output only. Compare identical stimulus windows and channels; acoustic and driver effects are not included."}


def replay_levels(manifest: Mapping, raw: Path, window_s: tuple[float, float]) -> dict:
    if manifest.get("schema") != "jts_dsp_replay/1" or sha256_file(raw) != manifest["render"]["output_sha256"]:
        raise ValueError("dsp_replay_output_identity_mismatch")
    start, stop = window_s
    if not 0 <= start < stop or stop - start > DEFAULT_MAX_CAPTURE_SECONDS:
        raise ValueError("dsp_replay_window_invalid")
    rate, channels = manifest["sample_rate_hz"], manifest["channels"]
    data = np.memmap(raw, dtype="<f8", mode="r").reshape(-1, channels)
    first, last = round(start * rate), round(stop * rate)
    if first < 0 or last > len(data) or last - first < 8:
        raise ValueError("dsp_replay_window_unavailable")
    bands = [(f"{lo:g}-{hi:g}", lo, hi) for lo, hi in BASS_BANDS_HZ]
    return {"schema": "jts_dsp_levels/1", "output_sha256": manifest["render"]["output_sha256"],
            "graph_sha256": manifest["graph_sha256"], "stimulus_sha256": manifest["stimulus_sha256"],
            "main_db": manifest["main_db"], "bass_reference_db": manifest["bass_reference_db"],
            "window_s": [first / rate, last / rate], "window": "rectangular",
            "channels": [{"channel": channel, "bands": band_levels_dbfs(data[first:last, channel], rate, bands, window="rectangular")}
                         for channel in range(channels)]}
