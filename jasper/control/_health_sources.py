# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The music-source label/unit tables shared across the audio-health leaves.

A leaf on purpose (see ``_health_fields``'s docstring for the pattern): the
composer, the source/timing cards and the incident-row builder all key off
the same source id <-> fanin label <-> systemd-unit mappings, and none of
them may import another to get it.
"""
from __future__ import annotations

from ..local_sources.registry import local_source_lifecycles
from ..music_sources import MUSIC_SOURCE_SPECS
from ._health_fields import DIAGNOSTICS_REMEDY, RESTART_REMEDY

_LABEL_TO_SOURCE = {
    spec.fanin_label: spec.id.value for spec in MUSIC_SOURCE_SPECS
}
SOURCE_LABELS = {
    spec.id.value: spec.display_name for spec in MUSIC_SOURCE_SPECS
}
_SOURCE_HEALTH_UNITS = {
    lifecycle.source.value: lifecycle.health_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_OFF_DRIFT_UNITS = {
    lifecycle.source.value: lifecycle.park_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_PRIMARY_UNITS = {
    lifecycle.source.value: (
        lifecycle.intent_unit
        or (lifecycle.runtime_units[0] if lifecycle.runtime_units else None)
    )
    for lifecycle in local_source_lifecycles()
}

# The one household-facing sentence for a source whose renderer has failed.
# Which unit failed and how belongs to doctor's per-renderer checks.
SOURCE_UNAVAILABLE_DETAIL = (
    f"JTS could not start this source. {RESTART_REMEDY} {DIAGNOSTICS_REMEDY}"
)

# ...and for a source still running after the household turned it Off. Saving
# the choice again is what re-runs the reconciler that stops it.
SOURCE_OFF_DRIFT_DETAIL = (
    "It is still running even though Playback sources has it turned off. Set it "
    "to Off again in Playback sources to clear this."
)
