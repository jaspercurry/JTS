# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
)
from jasper.camilla_emit import emit_gain_filter, emit_mixer

from ..camilla_names import output_commission_mute_name
from .devices import _camilla_latency, _finite_float, _positive_int, _yaml_string
from .document import _atomic_write_text
from .filters import STARTUP_MUTE_GAIN_DB
from .gates import _assert_parked_outputs_muted, _assert_volume_limit

# The PARKED graph's on-disk name + internal vocabulary — a generated,
# topology-derived, all-muted boot graph. See emit_active_speaker_parked_config
# for what parked means.
PARKED_CONFIG_NAME = "active_speaker_parked.yml"

PARKED_SILENCE_MIXER = "parked_silence"


# The parked graph's sink: a ``File`` playback, never a DAC — no DAC attached
# means no driver to over-drive, and it makes parking DAC-agnostic (a board with
# no active outputd lane at all can still park).
PARKED_SINK_PATH = "/dev/null"


# The `# Source:` marker the classifier keys on to recognise a parked graph.
# The emitter owns its own spelling; the runtime verifier re-declares it
# independently, exactly as ACTIVE_BASELINE_SOURCE is.
ACTIVE_PARKED_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_parked_config"
)


def emit_active_speaker_parked_config(
    *,
    output_count: int,
    topology_id: str | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
) -> str:
    """Build the PARKED (all-muted, DAC-less) active-speaker graph.

    The third statefile-seeding outcome, for a box whose saved topology declares
    roleful outputs but has not yet staged an all-muted startup graph: a flat
    full-range graph would put program into a declared tweeter, and refusing
    failed the whole deploy. This graph is silent twice over:

    * **The sink is a ``File``, not a DAC** (:data:`PARKED_SINK_PATH`) — no DAC
      attached, so no driver can be over-driven regardless of the saved
      topology, and parking works on a board with no active outputd lane at all.
      Its ``format`` is ALWAYS ``DEFAULT_PIPE_SINK_FORMAT``.
    * **Every physical output is hard muted** by the repo's one mute idiom — a
      ``Gain`` at :data:`STARTUP_MUTE_GAIN_DB` with ``mute: true`` — and the
      mixer feeds every destination at that same -120 dB floor, so even a
      defeated boolean mute is inaudible.

    It claims NOTHING else: no crossover, no driver roles, no per-driver
    protection, no limiter policy. ``runtime_contract._parked_graph_allowed``
    re-proves both properties before this graph may be selected.
    """

    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    output_count = _positive_int(output_count, "output_count")
    # playback_device=None because this sink is a clockless /dev/null File: it
    # declares no ALSA buffer, so the CAPTURE end owns the geometry. See
    # resolve_camilla_latency_for_devices.
    chunksize, target_level, queuelimit = _camilla_latency(
        capture_device, None, chunksize, target_level, None
    )
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    _assert_volume_limit(volume_limit_db)

    filter_lines: list[str] = []
    pipeline_lines = [
        "  - type: Mixer",
        f"    name: {PARKED_SILENCE_MIXER}",
    ]
    for index in range(output_count):
        mute_name = output_commission_mute_name(index)
        filter_lines.extend(
            emit_gain_filter(mute_name, STARTUP_MUTE_GAIN_DB, mute=True)
        )
        pipeline_lines.extend([
            "  - type: Filter",
            f"    channels: [{index}]",
            f"    names: [{mute_name}]",
        ])
    filter_yaml = "\n".join(filter_lines)
    pipeline_yaml = "\n".join(pipeline_lines)
    mixer_yaml = emit_mixer(
        PARKED_SILENCE_MIXER,
        channels_in=2,
        channels_out=output_count,
        mapping=[
            # Capture channel 0 only, at the mute floor. The mapping exists to
            # change the channel count, not to carry program: nothing audible
            # may ever reach a declared driver from a parked graph.
            (index, [(0, STARTUP_MUTE_GAIN_DB, False)])
            for index in range(output_count)
        ],
        description="parked: every output muted, no crossover claimed",
    )
    metadata_comments = []
    if topology_id:
        metadata_comments.append(
            f"# topology_id={_yaml_string(topology_id, 'topology_id')}"
        )
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker PARKED config.
# Source: {ACTIVE_PARKED_SOURCE}
{metadata_yaml}
# DO NOT HAND-EDIT. The saved output topology declares roleful/protected
# outputs but no all-muted active startup graph has been staged yet, so this
# graph parks every physical output hard-muted behind a File sink. It claims no
# crossover and no driver protection; it exists so the speaker holds SILENCE
# instead of running an illegal full-range graph. Finish crossover preview to
# stage a startup graph, or reset output setup and choose an explicit passive
# layout.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: File
    channels: {output_count}
    filename: "{PARKED_SINK_PATH}"
    format: {DEFAULT_PIPE_SINK_FORMAT}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # Fail-closed emit gate: re-prove against the EMITTED TEXT (not the
    # emitter's construction) that every physical output is hard-muted and that
    # mute is wired to its channel.
    _assert_parked_outputs_muted(yaml, output_count)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
    return yaml
