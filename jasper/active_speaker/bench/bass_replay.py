# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Attribute bass output changes using the existing native file renderer."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from jasper.audio_measurement.snr_policy import DBFS_FLOOR
from jasper.bass_extension.dynamic import as_dynamic_bass_descriptor
from jasper.bass_extension.dynamic_graph import build_native_dynamic_bass_graph, dynamic_bass_owner_groups, validated_base_graph

from ..profile import ActiveSpeakerPreset
from .replay import replay_graph, replay_levels


def replay_bass(graph: Path, stimulus: Path, out: Path, *, main_db: float,
                bass_reference_db: float | None = None, descriptor: Mapping, channels: tuple[int, ...],
                preset: ActiveSpeakerPreset | None = None) -> dict:
    settings = as_dynamic_bass_descriptor(descriptor)
    source = yaml.safe_load(graph.read_text())
    groups = dynamic_bass_owner_groups(channels, (
        (output.side, output.driver_role, output.output_variant, output.index)
        for output in (preset.channel_map.outputs if preset is not None else ())
    ))
    baseline = validated_base_graph(source, settings, channels, groups)
    fragment = build_native_dynamic_bass_graph(channels=source['devices']['playback']['channels'],
                                              owner_channels=channels, descriptor=settings, owner_groups=groups)
    uncompressed = {**source, 'pipeline': [step for step in source['pipeline']
        if not (step.get('type') == 'Processor' and step.get('name') in fragment.processors)]}
    out.mkdir(parents=True, exist_ok=True)
    stages = {}
    for name, payload in (('baseline', baseline), ('full_boost', uncompressed)):
        stage = out / name
        stage.mkdir(exist_ok=True)
        stage_graph = stage / 'graph.yml'
        stage_graph.write_text(yaml.safe_dump(payload, sort_keys=False))
        stages[name] = replay_graph(stage_graph, stimulus, stage, main_db=main_db,
                                    bass_reference_db=bass_reference_db)
    actual = replay_graph(graph, stimulus, out, main_db=main_db, bass_reference_db=bass_reference_db)
    return {**actual, 'bass_attribution': {'descriptor': settings.payload(), 'channels': list(channels),
        'stages': stages,
        'scope': 'Native file comparisons at final output, with downstream limiters retained. '
                 'Compressor effect is the net output change, not internal gain reduction or a physical limit.'}}


def bass_replay_levels(manifest: Mapping, raw: Path, window_s: tuple[float, float]) -> dict:
    attribution = manifest['bass_attribution']
    stages = attribution['stages']
    if 'volume_taper' in stages:
        # Rendered before ADR-0359: its delivered output includes the taper, so no stage isolates the compressor.
        raise ValueError('bass_replay_manifest_predates_adr_0359')
    readings = {'delivered': replay_levels(manifest, raw, window_s)}
    for name in ('baseline', 'full_boost'):
        stage = stages[name]
        if any(stage[key] != manifest[key] for key in ('stimulus_sha256', 'main_db', 'channels', 'sample_rate_hz')):
            raise ValueError('bass_replay_context_mismatch')
        readings[name] = replay_levels(stage, raw.parent / name / 'output.f64le', window_s)
    channels = []
    for channel in readings['delivered']['channels']:
        index = channel['channel']
        bands = []
        for band_index, band in enumerate(channel['bands']):
            values = {name: reading['channels'][index]['bands'][band_index]['level_dbfs']
                      for name, reading in readings.items()}
            def change(after, before):
                return round(values[after] - values[before], 3) if min(values[after], values[before]) > DBFS_FLOOR else None
            bands.append({**band, 'stage_levels_dbfs': values,
                'full_boost_gain_db': change('full_boost', 'baseline'),
                'compressor_output_change_db': change('delivered', 'full_boost'),
                'delivered_gain_db': change('delivered', 'baseline')})
        channels.append({'channel': index, 'bass_owner': index in attribution['channels'],
                         'ladder': channel['ladder'], 'bands': bands})
    return {**readings['delivered'], 'channels': channels, 'bass_attribution': {
        'descriptor': attribution['descriptor'], 'scope': attribution['scope'],
        'stages': {name: {key: reading[key] for key in ('output_sha256', 'graph_sha256', 'bass_reference_db')}
                   for name, reading in readings.items()}}}
