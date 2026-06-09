"""Emit a CamillaDSP correction config from a list of PEQ filters.

The correction config is structurally identical to the outputd Camilla
baseline except that the per-channel `Filter` blocks in the
pipeline now chain through the PEQs before the existing `flat` filter,
and the PEQs themselves are added to the `filters` block. The
`master_gain` mixer is preserved unchanged — Ducker still attenuates
`main_volume` for voice sessions.

We emit YAML by string concatenation rather than via a yaml library:
  - The structure is fixed and small.
  - Avoids adding a `pyyaml` / `ruamel.yaml` runtime dep just to
    write a deterministic small file.
  - The output is easy to review by eye; trivial to diff against the
    outputd base config when something looks wrong.

When CamillaDSP loads the file via SetConfigName + Reload, it does
the actual biquad coefficient generation from (freq, q, gain).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    DEFAULT_VOLUME_LIMIT_DB,
)
from jasper.camilla_emit import emit_peaking_biquad

from .peq import PEQ

logger = logging.getLogger(__name__)


def _emit_filter_definitions(peqs: Iterable[PEQ]) -> str:
    """Indented YAML for the `filters:` block.

    Always includes the `flat` Gain filter so the pipeline always
    has a terminator that matches the outputd base config. The PEQs are named
    `peq_1` through `peq_N` in the order returned by the designer
    (largest impact first).
    """
    lines = []
    # Preserve the existing `flat` identity filter so base ↔
    # correction.yml diff stays minimal and any other code paths
    # that referenced `flat` (none today, but stay open to future
    # composability) continue to work. Kept INLINE (not via
    # emit_gain_filter) on purpose: it is byte-matched to the base
    # cutover config's `gain: 0.0`, where the shared emitter would
    # write the 4-decimal `gain: 0.0000` and widen that diff.
    lines.append("  flat:")
    lines.append("    type: Gain")
    lines.append(
        "    parameters: { gain: 0.0, inverted: false, mute: false }"
    )

    for i, peq in enumerate(peqs, start=1):
        lines.extend(
            emit_peaking_biquad(f"peq_{i}", freq=peq.freq, q=peq.q, gain=peq.gain)
        )
    return "\n".join(lines)


def _emit_pipeline(peqs: list[PEQ]) -> str:
    """Indented YAML for the `pipeline:` block.

    Pipeline order:
      1. Mixer master_gain   (identity; placeholder for future mixer ops)
      2. Per-channel Filter   (PEQs → flat)

    The two per-channel Filter entries are duplicated because v1
    correction is mono — the same PEQ chain on both channels. Phase 2
    multi-position MMM might want per-channel correction; until then,
    the single-mic measurement only justifies a mono filter set.
    """
    chain_names = [f"peq_{i}" for i in range(1, len(peqs) + 1)] + ["flat"]
    # YAML flow-style list of bare identifiers — valid because the
    # names are simple alphanumeric+underscore.
    chain_str = "[" + ", ".join(chain_names) + "]"

    return (
        "  - type: Mixer\n"
        "    name: master_gain\n"
        "  - type: Filter\n"
        "    channels: [0]\n"
        f"    names: {chain_str}\n"
        "  - type: Filter\n"
        "    channels: [1]\n"
        f"    names: {chain_str}"
    )


def emit_correction_config(
    peqs: list[PEQ],
    *,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    playback_device: str = DEFAULT_PLAYBACK_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    measurement_id: str | None = None,
    enable_rate_adjust: bool = True,
) -> str:
    """Build a CamillaDSP YAML config with the given PEQ chain.

    Args:
      peqs: list of PEQ filters from jasper.correction.peq.design_peq.
        Empty list ⇒ identity config for the outputd path.
      capture_device, playback_device, capture_format, playback_format,
        sample_rate, chunksize, target_level, volume_limit_db: device,
        sample-rate, and safety config. Defaults match the outputd base
        config; override only if the audio path changes.
      out_path: write the YAML here as well as returning it. Parent
        directory must exist.
      measurement_id: opaque tag (e.g. timestamp) embedded in the
        YAML header comment so a `cat correction_*.yml` lineup is
        debuggable later.

    Returns:
      The YAML as a string (and writes to out_path if given).
    """
    filters_yaml = _emit_filter_definitions(peqs)
    pipeline_yaml = _emit_pipeline(peqs)

    # inv-5: a grouped member runs rate_adjust off (snapclient is the sole
    # rate-tracker). Caller passes enable_rate_adjust=False for an active
    # bond member; default True keeps the solo path unchanged.
    rate_adjust_literal = "true" if enable_rate_adjust else "false"
    header_id = f" (id={measurement_id})" if measurement_id else ""
    yaml = f"""---
# Auto-generated room-correction config{header_id}.
# Source: jasper.correction.camilla_yaml.emit_correction_config
# DO NOT HAND-EDIT — re-run a measurement at https://jts.local/correction
# instead. See docs/HANDOFF-correction.md for the architecture.
#
# Structure mirrors deploy/camilladsp/outputd-cutover.yml. The only
# differences are the PEQ filter additions in the `filters:` block and
# the matching name list in the per-channel `Filter` pipeline entries.
# `master_gain` mixer is preserved unchanged so the Ducker (voice
# session attenuation) keeps working without coordination.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: {rate_adjust_literal}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: 2
    device: "{playback_device}"
    format: {playback_format}

filters:
{filters_yaml}

mixers:
  master_gain:
    channels: {{ in: 2, out: 2 }}
    mapping:
      - dest: 0
        sources: [{{ channel: 0, gain: 0, inverted: false }}]
      - dest: 1
        sources: [{{ channel: 1, gain: 0, inverted: false }}]

pipeline:
{pipeline_yaml}
"""

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        out_path.write_text(yaml)
        logger.info("wrote correction config: %s (peqs=%d)", out_path, len(peqs))

    return yaml
