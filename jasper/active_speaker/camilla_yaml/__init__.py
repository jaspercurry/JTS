# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit commissioning-safe CamillaDSP templates for active speakers.

This module is intentionally side-effect-light: it can build or write a
candidate YAML file, but it does not ask CamillaDSP to load it. Hardware
activation belongs behind path-safety gates.
"""

from ..profile import ActiveSpeakerConfigError as ActiveSpeakerConfigError
from .decorate_dynamic_bass import (
    _dynamic_bass_graph as _dynamic_bass_graph,
    _with_dynamic_bass as _with_dynamic_bass,
)
from .decorate_protection import _add_baseline_protection as _add_baseline_protection
from .decorate_rear import (
    _mute_unfitted_rear_outputs as _mute_unfitted_rear_outputs,
    _rear_calibration_graph as _rear_calibration_graph,
    _rear_stage_channels as _rear_stage_channels,
    _validated_rear_calibration as _validated_rear_calibration,
)
from .devices import (
    FORBIDDEN_ACTIVE_PLAYBACK_TOKENS as FORBIDDEN_ACTIVE_PLAYBACK_TOKENS,
    ActiveEmitDevices as ActiveEmitDevices,
    _assert_ring_playback_width as _assert_ring_playback_width,
    _camilla_latency as _camilla_latency,
    _finite_float as _finite_float,
    _positive_int as _positive_int,
    _yaml_string as _yaml_string,
    active_emit_devices as active_emit_devices,
    capture_device_for_playback as capture_device_for_playback,
    forbidden_playback_token as forbidden_playback_token,
)
from .document import (
    _atomic_write_text as _atomic_write_text,
    _reserialize_keeping_header as _reserialize_keeping_header,
    logger as logger,
)
from .emit_baseline import emit_active_speaker_baseline_config as emit_active_speaker_baseline_config
from .emit_commissioning import emit_active_speaker_commissioning_config as emit_active_speaker_commissioning_config
from .emit_driver_domain import (
    DRIVER_DOMAIN_PROGRAM_CHANNELS as DRIVER_DOMAIN_PROGRAM_CHANNELS,
    emit_active_speaker_driver_domain_config as emit_active_speaker_driver_domain_config,
)
from .emit_parked import (
    ACTIVE_PARKED_SOURCE as ACTIVE_PARKED_SOURCE,
    PARKED_CONFIG_NAME as PARKED_CONFIG_NAME,
    PARKED_SILENCE_MIXER as PARKED_SILENCE_MIXER,
    PARKED_SINK_PATH as PARKED_SINK_PATH,
    emit_active_speaker_parked_config as emit_active_speaker_parked_config,
)
from .emit_program import (
    _PROGRAM_PROTECTION_RE as _PROGRAM_PROTECTION_RE,
    emit_active_speaker_program_config as emit_active_speaker_program_config,
    protected_neutral_program_origin as protected_neutral_program_origin,
)
from .emit_program_bake import (
    _PROGRAM_BAKE_SOURCE_LINE as _PROGRAM_BAKE_SOURCE_LINE,
    _SOUND_SOURCE_LINE as _SOUND_SOURCE_LINE,
    ACTIVE_PROGRAM_BAKE_SOURCE as ACTIVE_PROGRAM_BAKE_SOURCE,
    emit_active_speaker_program_bake_config as emit_active_speaker_program_bake_config,
)
from .emit_startup import emit_active_speaker_startup_config as emit_active_speaker_startup_config
from .filters import (
    _BLEND_CORRECTION_BIQUAD_TYPES as _BLEND_CORRECTION_BIQUAD_TYPES,
    APPLIED_RESPONSE_FILTER_MODE as APPLIED_RESPONSE_FILTER_MODE,
    BASELINE_LIMITER_CLIP_LIMIT_DB as BASELINE_LIMITER_CLIP_LIMIT_DB,
    COMMISSIONING_FILTER_MODE as COMMISSIONING_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB as COMMISSIONING_HEADROOM_DB,
    LINEARIZATION_BIQUAD_TYPES as LINEARIZATION_BIQUAD_TYPES,
    MAX_BLEND_CORRECTION_FILTERS as MAX_BLEND_CORRECTION_FILTERS,
    MAX_BLEND_CORRECTION_GAIN_DB as MAX_BLEND_CORRECTION_GAIN_DB,
    MAX_LINEARIZATION_FILTERS_PER_DRIVER as MAX_LINEARIZATION_FILTERS_PER_DRIVER,
    STARTUP_HEADROOM_DB as STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB as STARTUP_LIMITER_CLIP_LIMIT_DB,
    _blend_correction_name as _blend_correction_name,
    _crossover_filter_name as _crossover_filter_name,
    _driver_linearization_chain_names as _driver_linearization_chain_names,
    _driver_mute_name as _driver_mute_name,
    _emit_baseline_driver_definitions as _emit_baseline_driver_definitions,
    _emit_baseline_filter_definitions as _emit_baseline_filter_definitions,
    _emit_bass_management_hp_definition as _emit_bass_management_hp_definition,
    _emit_commissioning_filter_definitions as _emit_commissioning_filter_definitions,
    _emit_delay_filter as _emit_delay_filter,
    _emit_driver_linearization_definitions as _emit_driver_linearization_definitions,
    _emit_filter_definitions as _emit_filter_definitions,
    _emit_limiter_filter as _emit_limiter_filter,
    _emit_sub_baseline_definitions as _emit_sub_baseline_definitions,
    _emit_sub_commissioning_definitions as _emit_sub_commissioning_definitions,
    _emit_sub_startup_definitions as _emit_sub_startup_definitions,
    _program_protection_name as _program_protection_name,
    _protective_tweeter_hp_frequency as _protective_tweeter_hp_frequency,
    _room_peq_name as _room_peq_name,
    _sub_startup_mute_name as _sub_startup_mute_name,
    _validate_linearization_shelf_structure as _validate_linearization_shelf_structure,
    _validated_biquad_entry as _validated_biquad_entry,
    _validated_blend_correction as _validated_blend_correction,
    _validated_driver_corrections as _validated_driver_corrections,
    _validated_linearization as _validated_linearization,
    crossover_highpass_for_role as crossover_highpass_for_role,
    linearization_slot as linearization_slot,
)
from .gates import (
    EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR as EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR,
    PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE as PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE,
    _assert_graph_references_closed as _assert_graph_references_closed,
    _assert_measurement_delays_bound as _assert_measurement_delays_bound,
    _assert_parked_outputs_muted as _assert_parked_outputs_muted,
    _assert_pipeline_references_closed as _assert_pipeline_references_closed,
    _assert_program_graph_proven as _assert_program_graph_proven,
    _assert_tweeter_crossover_honours_declared_floor as _assert_tweeter_crossover_honours_declared_floor,
    _assert_tweeter_crossover_hp_satisfies_floor as _assert_tweeter_crossover_hp_satisfies_floor,
    _assert_tweeter_outputs_protected as _assert_tweeter_outputs_protected,
    _assert_view_tweeters_protected as _assert_view_tweeters_protected,
    _assert_volume_limit as _assert_volume_limit,
    _validate_program_role_channels as _validate_program_role_channels,
    preset_target_ids as preset_target_ids,
)
from .ledger import (
    BASELINE_HEADROOM_DB as BASELINE_HEADROOM_DB,
    MAX_PROGRAM_HEADROOM_DB as MAX_PROGRAM_HEADROOM_DB,
    _branch_context as _branch_context,
    _correction_bool as _correction_bool,
    _correction_value as _correction_value,
    boost_headroom_by_role as boost_headroom_by_role,
    linearization_has_boost as linearization_has_boost,
    linearization_headroom_db as linearization_headroom_db,
    program_headroom_db as program_headroom_db,
)
from .pipeline import (
    _commissioning_driver_filter_chain as _commissioning_driver_filter_chain,
    _driver_baseline_filter_chain as _driver_baseline_filter_chain,
    _driver_filter_chain as _driver_filter_chain,
    _emit_baseline_pipeline as _emit_baseline_pipeline,
    _emit_commissioning_pipeline as _emit_commissioning_pipeline,
    _emit_driver_domain_pipeline as _emit_driver_domain_pipeline,
    _emit_pipeline as _emit_pipeline,
    _emit_role_routed_mixer as _emit_role_routed_mixer,
    _emit_split_mixer as _emit_split_mixer,
    _mixer_sources as _mixer_sources,
    _sub_baseline_filter_chain as _sub_baseline_filter_chain,
    _sub_baseline_pipeline_lines as _sub_baseline_pipeline_lines,
    _sub_commissioning_filter_chain as _sub_commissioning_filter_chain,
    _sub_startup_filter_chain as _sub_startup_filter_chain,
    _validated_inverted_roles as _validated_inverted_roles,
    _validated_measurement_trims as _validated_measurement_trims,
    channel_select_mixer_name as channel_select_mixer_name,
    program_channel_count as program_channel_count,
)
from .topology import (
    _bass_management_active as _bass_management_active,
    _channels_for_role as _channels_for_role,
    _ordered_regions as _ordered_regions,
    _output_count as _output_count,
    audible_outputs_for_role as audible_outputs_for_role,
    role_polarity as role_polarity,
)
