# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    SHELF_Q,
    SHELF_Q_EMIT_DECIMALS,
    FilterSpec,
    PeqFilter,
    resolve_enable_rate_adjust,
)
from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT

from ..graph_safety import view_from_yaml_dict
from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
from .decorate_dynamic_bass import _dynamic_bass_graph, _with_dynamic_bass
from .decorate_protection import _add_baseline_protection
from .decorate_rear import (
    _mute_unfitted_rear_outputs,
    _rear_calibration_graph,
    _validated_rear_calibration,
)
from .devices import (
    _assert_ring_playback_width,
    _camilla_latency,
    _finite_float,
    _positive_int,
    _yaml_string,
    forbidden_playback_token,
)
from .document import _atomic_write_text, _reserialize_keeping_header, logger
from .filters import (
    BASELINE_LIMITER_CLIP_LIMIT_DB,
    _blend_correction_name,
    _emit_baseline_filter_definitions,
    _linearization_slot,
    _room_peq_name,
    _validated_blend_correction,
    _validated_driver_corrections,
    _validated_linearization,
)
from .gates import (
    _assert_graph_references_closed,
    _assert_pipeline_references_closed,
    _assert_tweeter_crossover_honours_declared_floor,
    _assert_tweeter_outputs_protected,
    _assert_view_tweeters_protected,
    _assert_volume_limit,
)
from .ledger import BASELINE_HEADROOM_DB
from .pipeline import _emit_baseline_pipeline, _emit_split_mixer
from .topology import _output_count


def emit_active_speaker_baseline_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    baseline_headroom_db: float = BASELINE_HEADROOM_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    queuelimit: int | None = None,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    bass_extension: Mapping[str, Any] | None = None,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    blend_correction: Sequence[Mapping[str, Any]] | None = None,
    rear_calibration: Mapping[str, Any] | None = None,
    excited_target_ids: Collection[str] = (),
) -> str:
    """Build an accepted active-speaker baseline candidate.

    Unlike the startup template this YAML is not muted. It still preserves the
    JTS 0 dB volume ceiling, keeps per-driver limiters, and refuses positive
    per-driver correction gain; callers own the acceptance evidence and the
    explicit CamillaDSP apply step.

    Every program-domain layer is emitted on channels [0, 1] strictly BEFORE the
    split mixer, upstream of every crossover, limiter and tweeter high-pass:

    * ``room_peqs`` (Layer B) — the preserved room-correction PEQ set; any
      positive boost is folded into ``active_baseline_headroom``.
    * ``preference_filters`` (Layer C) — the same ``FilterSpec`` objects the
      stereo emitter takes, emitted VERBATIM (dropping neutral bands is the
      caller's job, because the live editing draft needs its idle slots).
      Preference boosts ride at unity, matching ``emit_sound_config``.
    * ``output_trim_db`` — the household's manual headroom + loudness-match
      attenuation, folded into the same headroom gain, applied only when some
      band actually boosts.
    * ``blend_correction`` — the crossover blend region's summed-response-owned
      shape correction, flat rather than per-role because it describes the SUM;
      see ``_emit_baseline_pipeline`` for what that placement buys.

    ``rear_calibration`` is the ``jts_rear_calibration`` electrical document for
    this cabinet. Present, it compiles to the cardioid stage spliced straight
    after the split mixer, upstream of every role chain, and the rear output
    plays; absent, the rear output stays terminally muted.

    ``linearization`` (Layer 1a) is the per-driver stage the fit engine designs,
    in the REDUCED shape ``{role: [{biquad_type, freq, q, gain}, ...]}``. Each
    role's filters are emitted immediately after that driver's crossover HP/LP
    and before bass-extension.

    ``linearization`` and ``blend_correction`` are both independently
    re-validated here (``_validated_linearization`` /
    ``_validated_blend_correction``) rather than trusted from the caller — the
    per-filter boost cap and shelf-placement structure for one, Peaking-only and
    NON-POSITIVE gain for the other. Every empty default keeps an existing
    caller byte-identical.
    """

    preset.validate()
    # L0 emit gate (fail-closed), BEFORE any YAML is built: this is the graph
    # the routine apply transaction ships to a household, so a crossover below
    # the tweeter's declared protection floor is refused here rather than left
    # for the startup-load gate to catch on the next boot.
    _assert_tweeter_crossover_honours_declared_floor(preset)
    linearization = linearization or {}
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker baselines require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize, target_level, queuelimit = _camilla_latency(
        capture_device, playback_device, chunksize, target_level, queuelimit
    )
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    baseline_headroom_db = _finite_float(baseline_headroom_db, "baseline_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    output_trim_db = _finite_float(output_trim_db, "output_trim_db")
    _assert_volume_limit(volume_limit_db)
    if baseline_headroom_db < 0 or baseline_headroom_db > 40:
        raise ActiveSpeakerConfigError("baseline_headroom_db must be between 0 and 40")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections = _validated_driver_corrections(preset, corrections)
    safe_linearization = _validated_linearization(preset, linearization)
    safe_blend_correction = _validated_blend_correction(blend_correction)
    safe_rear_calibration = _validated_rear_calibration(rear_calibration, sample_rate=sample_rate)

    emitted_preference_filters = tuple(preference_filters)
    room_peqs = tuple(room_peqs)

    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_yaml = _emit_baseline_filter_definitions(
        preset,
        baseline_headroom_db=baseline_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
        room_peqs=room_peqs,
        preference_filters=emitted_preference_filters,
        output_trim_db=output_trim_db,
        linearization=safe_linearization,
        blend_correction=safe_blend_correction,
        rear_calibration=safe_rear_calibration,
    )
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections`` (a per-driver Gain filter below), so the mixer must
    # stay a no-op inverter — see the docstring on _emit_split_mixer.
    mixer_yaml = _emit_split_mixer(preset, apply_region_polarity=False)
    pipeline_yaml = _emit_baseline_pipeline(
        preset,
        room_peq_names=[_room_peq_name(i) for i in range(1, len(room_peqs) + 1)],
        preference_filter_names=[spec.name for spec in emitted_preference_filters],
        linearization=safe_linearization,
        blend_correction_names=[
            _blend_correction_name(i)
            for i in range(1, len(safe_blend_correction) + 1)
        ],
    )
    filter_yaml, pipeline_yaml = _add_baseline_protection(
        preset, filter_yaml, pipeline_yaml, protection_sections_by_role,
    )
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    metadata_yaml = "\n".join(metadata_comments)
    capture_yaml = f"""  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}"""

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker baseline config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config
{metadata_yaml}
# This is a candidate speaker baseline: crossover filters are active, outputs
# are not startup-muted, per-driver correction gain is non-positive, and the
# software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
{capture_yaml}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # The rear output plays only behind its own fitted stage; without one it stays
    # terminally muted (ADR-0318, issue #5161) — unless a measurement take names
    # it as an excited target, which is how the stage's own transfer gets
    # measured in the first place. Empty for every household graph.
    #
    # L0 emit gates (fail-closed) run on the FINAL graph so every decoration is
    # inside them: the durable (unmuted) baseline is what a household plays
    # through, so re-prove every tweeter output carries its crossover /
    # protective high-pass, and that the pipeline the baseline assembled from
    # independent helper calls references nothing undefined.
    if safe_rear_calibration:
        import yaml as yaml_lib  # lazy: the local `yaml` here is the emitted text

        # Read ONCE, decorated in place, dumped once below.
        graph = yaml_lib.safe_load(yaml)
        if bass_extension:
            graph = _dynamic_bass_graph(graph, preset, bass_extension)
        graph = _rear_calibration_graph(graph, preset, safe_rear_calibration)
        _assert_view_tweeters_protected(view_from_yaml_dict(graph), preset)
        _assert_graph_references_closed(graph, preset)
        yaml = _reserialize_keeping_header(yaml, graph)
    else:
        yaml = _mute_unfitted_rear_outputs(
            _with_dynamic_bass(yaml, preset, bass_extension), preset,
            excited_target_ids=excited_target_ids,
        )
        _assert_tweeter_outputs_protected(yaml, preset, decorated=bool(bass_extension))
        _assert_pipeline_references_closed(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        # linearization_shelves / shelf_q date, per speaker, the write after
        # which a persisted Layer-1a design realizes the Butterworth shelf Q the
        # fit designed it at rather than CamillaDSP's gain-dependent
        # ``slope: 6`` Q (0.476 at -11 dB) — an audible treble change.
        shelf_count = sum(
            1
            for filters in safe_linearization.values()
            for index in range(len(filters))
            if _linearization_slot(index, len(filters), filters) in ("shelf", "taper")
        )
        logger.info(
            "event=active_speaker_baseline_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d "
            "linearization_shelves=%d shelf_q=%.*f",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            shelf_count,
            SHELF_Q_EMIT_DECIMALS,
            SHELF_Q,
        )
    return yaml
