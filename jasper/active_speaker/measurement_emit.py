# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measurement graph's ONE emit, with no host in it.

:class:`~jasper.active_speaker.crossover_v2.session_graph.MeasurementSessionGraph`
takes an ``emit`` callable over the three measurement VARIANT axes. This module
is that callable, and the snapshot of the applied speaker it reads.

**It lived inside ``jasper.web.correction_crossover_v2`` as a closure**, which
made an 8,000-line web host a required import for anything else that wanted to
measure through the same graph. Nothing about the emit is web vocabulary: it is
a preset, a topology, a role→channel map, a sink and the confirmed per-role
protection. So the closure variables became :class:`MeasurementGraphProfile` and
the function moved here, where the wizard and a CLI door reach the same one.

Every variant pays the emitter's own fail-closed proofs
(``_assert_program_graph_proven``), unchanged: a flipped, delayed or levelled
branch is not a way past them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "MeasurementGraphProfile",
    "emit_measurement_graph",
]


@dataclass(frozen=True)
class MeasurementGraphProfile:
    """The applied speaker, as one measurement session's emit reads it.

    Everything a measurement graph needs that is NOT a per-measurement choice.
    Frozen and session-scoped: these five are what made the old emit a closure,
    and they are the fact that makes the graph a session constant rather than a
    per-stimulus one.

    ``protection_sections_by_role`` is the CONFIRMED per-role protection — the
    tweeter high-pass that the emitter pairs with the per-driver limiter. It is
    read from the applied profile on the box, never stated by a caller: a
    measurement graph carrying protection some operator typed is a measurement
    graph whose safety argument nobody checked.
    """

    preset: Any
    topology: Any
    role_channels: Mapping[str, int]
    playback_device: str
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None


def emit_measurement_graph(
    profile: MeasurementGraphProfile,
    inverted_roles: tuple[str, ...] = (),
    measurement_delays_us: Mapping[str, float] | None = None,
    level_trims_db: Mapping[str, float] | None = None,
) -> str:
    """One session's measurement graph, per measurement variant.

    **Preference EQ is never in it, by construction.** A measurement plays
    through the layer under tune and everything BELOW it, never anything above
    (owner ruling, 2026-09-01, #3489). Preference EQ sits above every tunable
    layer, so it is not part of any measurement graph and not relevant to any
    tuning comparison. ``MeasurementGraphProfile`` carries preset, topology,
    role channels, playback device and protection sections — it has no field
    for a ``SoundProfile`` and this module reads none, so the household's taste
    cannot reach a capture even though the DURABLE graph now always carries
    preference slots (the fixed frame). That is not an accident of the current
    field list; it is the invariant, and
    ``tests/test_active_speaker_measurement_emit_excludes_preference_eq``
    fails if a field or a read is ever added.

    Everything but the three VARIANT axes — ``inverted_roles``,
    ``measurement_delays_us`` and ``level_trims_db`` — rides on ``profile``,
    which is what makes the graph session-scoped rather than per-stimulus: the
    old per-capture site emitted these same bytes for every stimulus. The three
    are what a measurement chooses — empty on every normal capture, which keeps
    that emit byte-identical to what it was.

    ``level_trims_db`` arrives RESOLVED. This function applies the numbers it is
    handed and never asks the evidence question itself: that question has one
    owner (``baseline_profile.measured_level_trims``) and is asked once, at
    session open, where a box with no evidence can still refuse the walk instead
    of silently measuring unmatched branches.

    BOTH HALVES OF THE DEVICE BLOCK, DERIVED TOGETHER (issue #2450).
    ``playback_device`` is already marker-aware — on an armed box
    ``resolve_active_playback_device`` answers the ACTIVE RING — but naming only
    the sink left the emitter to default its capture lane to the snd-aloop tap,
    which under ``shm_ring`` fan-in has stopped feeding: Stage 1's per-driver
    sweeps would excite the ring while CamillaDSP captured a device nobody
    writes. Digital silence, every daemon healthy, and no gate to catch it — the
    capture-channel check compares 2 == 2 and the arm's width gate only holds
    ring-NAMED lanes to the wire. ``active_emit_devices`` is the one place that
    answers for a ring PCM, so this site asks it rather than learning the ring;
    on every unarmed box it returns today's literals and this emit is
    byte-identical.

    EVERY field it derives is forwarded. A subset is the same defect one level up
    (#2343/#2359/#2363's family), which is why
    ``test_every_emit_devices_field_reaches_the_emitter`` walks
    ``dataclasses.fields`` at this site too — and it matters MORE at session
    scope, where a half-derived block would poison every stimulus rather than
    one.
    """
    from jasper.active_speaker.camilla_yaml import (
        active_emit_devices,
        emit_active_speaker_program_config,
    )

    devices = active_emit_devices(profile.playback_device, topology=profile.topology)
    return emit_active_speaker_program_config(
        profile.preset,
        role_channels=dict(profile.role_channels),
        playback_device=profile.playback_device,
        protection_sections_by_role=profile.protection_sections_by_role,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
        inverted_roles=inverted_roles,
        measurement_delays_us=measurement_delays_us,
        measurement_level_trims_db=level_trims_db,
    )
