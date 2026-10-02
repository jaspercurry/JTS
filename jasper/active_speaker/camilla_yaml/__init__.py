# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit commissioning-safe CamillaDSP templates for active speakers.

This module is intentionally side-effect-light: it can build or write a
candidate YAML file, but it does not ask CamillaDSP to load it. Hardware
activation belongs behind path-safety gates.
"""

from .decorate_rear import _rear_stage_channels as _rear_stage_channels
from .devices import (
    active_emit_devices as active_emit_devices,
    capture_device_for_playback as capture_device_for_playback,
    forbidden_playback_token as forbidden_playback_token,
)
from .document import _reserialize_keeping_header as _reserialize_keeping_header
from .emit_baseline import emit_active_speaker_baseline_config as emit_active_speaker_baseline_config
from .emit_commissioning import emit_active_speaker_commissioning_config as emit_active_speaker_commissioning_config
from .emit_parked import (
    ACTIVE_PARKED_SOURCE as ACTIVE_PARKED_SOURCE,
    PARKED_CONFIG_NAME as PARKED_CONFIG_NAME,
    emit_active_speaker_parked_config as emit_active_speaker_parked_config,
)
from .emit_program import (
    emit_active_speaker_program_config as emit_active_speaker_program_config,
    protected_neutral_program_origin as protected_neutral_program_origin,
)
from .emit_program_bake import (
    ACTIVE_PROGRAM_BAKE_SOURCE as ACTIVE_PROGRAM_BAKE_SOURCE,
    emit_active_speaker_program_bake_config as emit_active_speaker_program_bake_config,
)
from .emit_startup import emit_active_speaker_startup_config as emit_active_speaker_startup_config
from .filters import (
    APPLIED_RESPONSE_FILTER_MODE as APPLIED_RESPONSE_FILTER_MODE,
    BASELINE_LIMITER_CLIP_LIMIT_DB as BASELINE_LIMITER_CLIP_LIMIT_DB,
    COMMISSIONING_FILTER_MODE as COMMISSIONING_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB as COMMISSIONING_HEADROOM_DB,
    LINEARIZATION_BIQUAD_TYPES as LINEARIZATION_BIQUAD_TYPES,
    PROGRAM_HEADROOM_BINDING as PROGRAM_HEADROOM_BINDING,
    PROGRAM_HEADROOM_EXHAUSTED as PROGRAM_HEADROOM_EXHAUSTED,
    ProgramHeadroomExhausted as ProgramHeadroomExhausted,
    STARTUP_HEADROOM_DB as STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB as STARTUP_LIMITER_CLIP_LIMIT_DB,
    crossover_highpass_for_role as crossover_highpass_for_role,
    linearization_slot as linearization_slot,
)
from .gates import preset_target_ids as preset_target_ids
from .ledger import MAX_PROGRAM_HEADROOM_DB as MAX_PROGRAM_HEADROOM_DB
from .pipeline import (
    _emit_role_routed_mixer as _emit_role_routed_mixer,
    program_channel_count as program_channel_count,
)
from .topology import (
    _channels_for_role as _channels_for_role,
    _output_count as _output_count,
    audible_outputs_for_role as audible_outputs_for_role,
    role_polarity as role_polarity,
)
