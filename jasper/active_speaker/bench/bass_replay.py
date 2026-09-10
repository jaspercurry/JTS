# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Attribute bass output changes using the existing native file renderer."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from jasper.audio_measurement.snr_policy import DBFS_FLOOR
from jasper.bass_extension.dynamic import LOUDNESS_TAPER_DB, DynamicBassDescriptor, validate_dynamic_bass_descriptor
from jasper.bass_extension.dynamic_graph import build_native_dynamic_bass_graph, validated_base_graph

from .replay import replay_graph, replay_levels


def replay_bass(graph: Path, stimulus: Path, out: Path, *, main_db: float,
                bass_reference_db: float, descriptor: Mapping, channels: tuple[int, ...]) -> dict:
    descriptor = validate_dynamic_bass_descriptor(descriptor)
    settings = DynamicBassDescriptor(**descriptor)
    source = yaml.safe_load(graph.read_text())
    baseline = validated_base_graph(source, settings, channels)
    fragment = build_native_dynamic_bass_graph(channels=source['devices']['playback']['channels'],
                                              owner_channels=channels, descriptor=settings)
    uncompressed = {**source, 'pipeline': [step for step in source['pipeline']
        if not (step.get('type') == 'Processor' and step.get('name') in fragment.processors)]}
    out.mkdir(parents=True, exist_ok=True)
    stages = {}
    for name, payload, reference in (
        ('baseline', baseline, bass_reference_db),
        ('full_boost', uncompressed, settings.reference_level_db - LOUDNESS_TAPER_DB),
        ('volume_taper', uncompressed, bass_reference_db),
    ):
        stage = out / name
        stage.mkdir(exist_ok=True)
        stage_graph = stage / 'graph.yml'
        stage_graph.write_text(yaml.safe_dump(payload, sort_keys=False))
        stages[name] = replay_graph(stage_graph, stimulus, stage, main_db=main_db,
                                    bass_reference_db=reference)
    actual = replay_graph(graph, stimulus, out, main_db=main_db, bass_reference_db=bass_reference_db)
    return {**actual, 'bass_attribution': {'descriptor': dict(descriptor), 'channels': list(channels),
        'stages': stages,
        'scope': 'Native file comparisons at final output, with downstream limiters retained. '
                 'Compressor effect is the net output change, not internal gain reduction or a physical limit.'}}


def bass_replay_levels(manifest: Mapping, raw: Path, window_s: tuple[float, float]) -> dict:
    attribution = manifest['bass_attribution']
    stages = attribution['stages']
    readings = {'delivered': replay_levels(manifest, raw, window_s)}
    for name in ('baseline', 'full_boost', 'volume_taper'):
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
                'volume_taper_output_change_db': change('volume_taper', 'full_boost'),
                'compressor_output_change_db': change('delivered', 'volume_taper'),
                'delivered_gain_db': change('delivered', 'baseline')})
        channels.append({'channel': index, 'bass_owner': index in attribution['channels'], 'bands': bands})
    return {**readings['delivered'], 'channels': channels, 'bass_attribution': {
        'descriptor': attribution['descriptor'], 'scope': attribution['scope'],
        'stages': {name: {key: reading[key] for key in ('output_sha256', 'graph_sha256', 'bass_reference_db')}
                   for name, reading in readings.items()}}}
