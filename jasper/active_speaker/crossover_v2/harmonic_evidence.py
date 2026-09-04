# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read H2/H3 out of one round's banked MEASURE captures, and file the reading.

An OFFLINE instrument, the same shape as :mod:`.feature_classifier`: it reads
captures a round already banked, writes one artifact into that round's own
directory, and is then read by
:func:`~.evidence_packet.build_crossover_evidence_packet`. No Pi is touched
and nothing is re-measured. There is no ABSOLUTE level — the corpus banks no
SPL — so every number is "dB below the fundamental, at the drive this capture
used", and the drive rides beside it in dBFS; the two are never published
apart.

**The production window cannot be used.**
:data:`~jasper.audio_measurement.program_analysis.DECONV_PRE_GUARD_S` is
0.25 s, while the H3 image LEADS the linear IR by ``L·ln 3`` — about 1.34 s
on a MEASURE woofer sweep. Deconvolution is circular, so at the production
window every harmonic image is wrapped off the front of the array.
:func:`~jasper.audio_measurement.distortion.read_segment_distortion`
re-deconvolves the SAME capture bytes at
:func:`~jasper.audio_measurement.distortion.required_pre_guard_s`.

**Three gates, all of which refuse rather than report.** The MEASURE program
is REBUILT from the round's own banked ``gain_plan_db`` and driver bands and
must reproduce the ``program_id`` the session recorded — a SHA-256 over the
full segment schedule, so a match proves every ``L`` the harmonic offsets
derive from. The shipped ``analyze_program_capture`` is re-run and its
:data:`FIDELITY_FIELDS` compared against the sidecar's own ``diagnostic``
block, fail-closed: zero comparisons is not a passed gate, and the count
actually compared is published for every capture. :func:`_scope_captures`
decides which CAPTURES belong to the program, and refuses rather than
guessing — read its docstring for the one seam it does not close.

A third gate, and the one that is easiest to leave out
------------------------------------------------------
Neither gate above says which CAPTURES belong to the program.  Gate 1 proves
the program matches the state; gate 2's fields are every one of them
amplitude-invariant.  So a capture from a neighbouring round, read against this
round's program, publishes that program's drive — silently, and wrongly.
:func:`_scope_captures` is the gate for the CAPTURE half of that, and it
REFUSES rather than guessing.  It does **not** close the STATE half — a caller
handing it a scope from one round and a flow state from another mis-attributes
drive through every gate cleanly — and that residual is named in full in its
docstring, along with the one change that retires it.  Read it before treating
this module as all-clear.

**Provenance of the method.**  Both gates, the band arithmetic and the pooling
rule were established against the 2026-08-17 and 2026-08-19 corpora by a lab
bench this module replaced.  The math is not re-derived here — it is
:mod:`jasper.audio_measurement.distortion`'s, called.

**One seam worth naming.**  Locating the schedule inside a capture uses
``program_analysis``'s ``_global_offset`` / ``_locate_segments`` /
``_estimate_drift``.  Those are private, and importing them is deliberate: the
anchors a distortion read rides MUST be the anchors the session's own analysis
used, and re-deriving them through a public path would be a second answer to a
question that already has one.  The durable fix is for a capture to BANK its
anchors; until it does, reproducing them is the only way to be sure, and the
fidelity gate above is what proves the reproduction landed.
"""

from __future__ import annotations

import json
import math
import statistics
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..profile import DRIVER_ROLES_BY_WAY
from .evidence_packet import (
    HARMONICS_ARTIFACT,
    NO_ROUND_ARTIFACTS_REASON,
    RING_SIDECAR_GLOB,
    _applied_profile_source,
    round_artifact_dir,
)
from .journey import PHASE_MEASURE

__all__ = [
    "DEFAULT_BANDS_HZ",
    "DEFAULT_FULL_RANGE_BAND_HZ",
    "FIDELITY_FIELDS",
    "FIDELITY_TOLERANCE",
    "HARMONICS_ARTIFACT",
    "HARMONICS_ARTIFACT_KIND",
    "HARMONICS_SCHEMA_VERSION",
    "HARMONIC_ORDERS",
    "NO_ADMISSIBLE_CAPTURES",
    "NO_CAPTURE_PASSED_THE_GATES",
    "PROBE_FREQUENCIES_HZ",
    "PROGRAM_NOT_REPRODUCIBLE",
    "RING_NOT_SCOPED_TO_ONE_SESSION",
    "STATE_UNREADABLE",
    "HarmonicEvidenceRefused",
    "banked_roles",
    "read_bundle_harmonics",
    "read_round_harmonics",
    "rebuild_measure_program",
    "round_bands_hz",
]

#: Bumped when a field changes MEANING, never for an additive widening — the
#: rule :data:`~.evidence_packet.PACKET_SCHEMA_VERSION` keeps: a reader that
#: ignores what it does not know is not misled by a new key.
HARMONICS_SCHEMA_VERSION = 1

#: The artifact's own kind tag, so a file found loose says what it is.
HARMONICS_ARTIFACT_KIND = "jts_crossover_v2_harmonic_distortion"

#: The orders read. Not imported from
#: :data:`~jasper.audio_measurement.distortion.DEFAULT_HARMONIC_ORDERS`
#: even though it holds the same pair today: that constant bounds what the
#: ANALYSIS kernel will separate, this one is the product's choice of what
#: to publish.
HARMONIC_ORDERS: tuple[int, ...] = (2, 3)

#: The shipped MEASURE driver bands for a PAIR. A default, not a constant: a
#: wrong pair fails the ``program_id`` proof rather than misreading the round.
DEFAULT_BANDS_HZ: dict[str, tuple[float, float]] = {
    "woofer": (150.0, 4000.0),
    "tweeter": (1600.0, 20000.0),
}

#: The 1-way default: the whole measurable span in Hz, since a passive main's
#: own declared band is not derivable here.
DEFAULT_FULL_RANGE_BAND_HZ: tuple[float, float] = (150.0, 20000.0)

#: Excitation frequencies the rows are sampled at. A fixed ladder rather than
#: the full FFT grid, because this document is read by an LLM operator and
#: by a human at a terminal. Roughly third-octave from 150 Hz. Points
#: outside a role's own band are omitted rather than published as null, so
#: a row that exists is a row that was measured.
PROBE_FREQUENCIES_HZ: tuple[float, ...] = (
    150.0, 200.0, 300.0, 400.0, 600.0, 800.0, 1000.0,
    1500.0, 2000.0, 3000.0, 4000.0, 6000.0, 8000.0,
)

#: Session volumes tried when solving the program id: -40..0 dB in half-dB
#: steps, ~80 program builds per courtesy-prelude value, and
#: :func:`rebuild_measure_program` tries at most two.
#:
#: **This grid does NOT cover every value the flow can compose.**
#: ``session_volume_plan.session_measurement_volume_db`` returns an
#: UNQUANTIZED float over the open interval (-60, 0], so the grid covers
#: exactly the codified -20 dB default and a banked seat-level reference
#: that happens to land on a half-dB. Anything else fails the solve, and
#: :data:`PROGRAM_NOT_REPRODUCIBLE` names that cause explicitly. The
#: durable fix is for the round to BANK its session volume.
_DOWNSTREAM_GRID_DB: tuple[float, ...] = tuple(
    round(-40.0 + 0.5 * step, 1) for step in range(81)
)

#: Sidecar diagnostics compared against the replay. Each is a value the banked
#: analysis recorded about ITSELF, so a mismatch means the reconstruction
#: reads different bytes than the session did. Scoped to what a DISTORTION
#: read rides on: the clock-drift estimate divided out of the reference,
#: the repeat agreement, and the integrity flags.
#:
#: ``max_residual_samples`` and ``glitch_detected`` are deliberately ABSENT.
#: D7 (``b98e9380f``, 2026-08-18) replaced the estimator behind them, so
#: every capture banked before that commit records a value from the blunter
#: instrument and comparing either would report a deliberate product
#: improvement as a broken reconstruction. A banked-vs-replay disagreement
#: on ``glitch_detected`` is DISCLOSED per capture instead.
#: ``alignment_confidence`` / ``anchor_delay_us`` are absent for a
#: different reason: they are not deterministic across re-analyses of one
#: capture, so they cannot be a fidelity signal for anything.
FIDELITY_FIELDS: tuple[str, ...] = (
    "epsilon_ppm",
    "woofer_repeat_epsilon_ppm",
    "tweeter_repeat_epsilon_ppm",
    "repeat_level_delta_db",
    "linearity_ok",
)

#: How far a replayed fidelity field may sit from the banked one. The banked
#: values are rounded to 3 decimals by ``analysis_diagnostic_summary``, so
#: this is five times the rounding grain — tight enough that a different
#: capture cannot pass, loose enough that the rounding cannot fail.
FIDELITY_TOLERANCE = 5e-3

#: Decimal places the published dB figures carry. One, because the pooling
#: below is a median over sweeps whose own scatter is tenths of a dB.
#: Digits past it would be arithmetic noise in a document that is
#: content-fingerprinted downstream.
_DB_DECIMALS = 1

#: Decimal places for THD percent, which is a small number where the first
#: significant digit often sits three places in.
_PERCENT_DECIMALS = 3


#: The round banks no MEASURE capture this instrument can read.
NO_ADMISSIBLE_CAPTURES = "no_admissible_captures"

#: The MEASURE program could not be rebuilt to the id the session recorded.
PROGRAM_NOT_REPRODUCIBLE = "program_not_reproducible"

#: The flow state carries neither the program id nor the gain plan.
STATE_UNREADABLE = "state_unreadable"

#: Captures were found and every one of them failed a gate.
NO_CAPTURE_PASSED_THE_GATES = "no_capture_passed_the_gates"

#: No scope was supplied and the ring holds MEASURE captures this reader
#: cannot attribute to one session. See :func:`_scope_captures` for what
#: reading them together would silently publish.
RING_NOT_SCOPED_TO_ONE_SESSION = "ring_not_scoped_to_one_session"


class HarmonicEvidenceRefused(Exception):
    """This instrument declined to produce a reading, by name.

    ``reason`` is one of the module's refusal constants and ``evidence`` is
    what it saw. An exception rather than a partial artifact: a distortion
    document that quietly covered half a round would be read as the round's
    answer.
    """

    def __init__(self, reason: str, evidence: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence or {})


def _read_mono(path: Path) -> np.ndarray:
    """One WAV as mono float64 in [-1, 1), at whatever width it was written.

    Width comes from the container's own ``fmt`` chunk, never assumed: the dump
    ring holds 16-bit phone captures AND 32-bit wired captures, and reading one
    as the other is a 96 dB level error that would read as a distortion finding.
    """
    with wave.open(str(path)) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 2**15
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2**31
    else:
        raise ValueError(f"{path.name}: unsupported sample width {width} bytes")
    return samples[::channels] if channels > 1 else samples


def _banked_sweep_durations_s(
    state: Mapping[str, Any], bands: Mapping[str, tuple[float, float]]
) -> dict[str, float] | None:
    """The round's OWN realized per-role MEASURE sweep length, if it banked one.

    #2923: a round composed since #2921 may have had either sweep FITTED to a
    declared duration limit — a continuous float no search grid can reach — so
    a round that banked the realized length (``V2ConductorSnapshot.
    measure_sweep_durations_s``) is read back EXACTLY here instead of
    re-derived. ``None`` for every round banked before that field existed.

    **Every VALUE's own failure mode is fail-soft: an unusable value makes
    this return ``None``, never raise.** Malformed or partial values (wrong
    type, non-finite, non-positive, a missing role) are treated as absent, and
    so is a value too short to close one cycle at f1 — but the ceiling checked
    against is the band the COMPOSER will actually sweep,
    ``_intersect_band(band, MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ)``
    (150–23,000 Hz), not the raw declared one, so a datasheet-wide tweeter
    band at or above 24 kHz clamps DOWN before Nyquist (24,000 Hz at this
    module's 48 kHz sample rate) ever enters the question. Both the kernel
    function and the intersection are imported rather than restated, so
    neither rule can drift into a second copy.
    """
    raw = state.get("measure_sweep_durations_s")
    if not isinstance(raw, Mapping):
        return None
    from jasper.audio_measurement.program import (
        MEASURE_SWEEP_F_HI_HZ,
        MEASURE_SWEEP_F_LO_HZ,
        PROGRAM_SAMPLE_RATE_HZ,
        FrequencyBand,
        _intersect_band,
    )
    from jasper.audio_measurement.sweep import synchronized_sweep_metadata

    durations: dict[str, float] = {}
    for role in bands:
        value = raw.get(role)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value <= 0.0:
            return None
        # The band the composer will actually sweep over — the same clamp, constants
        # and function ``build_measure_program``'s own ``_band()`` closure calls.
        # Deliberately OUTSIDE the try/except below: a ``bands`` argument that does
        # not intersect the measurement window at all is not a malformed VALUE
        # this function fails soft over, and the caller's own unconditional
        # ``RoleBand(...)`` reaches the same intersection unguarded moments later.
        f1, f2 = _intersect_band(
            FrequencyBand(*bands[role]), MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ,
        )
        try:
            synchronized_sweep_metadata(
                f1=f1, f2=f2, duration_approx_s=float(value),
                sample_rate=PROGRAM_SAMPLE_RATE_HZ,
            )
        except ValueError:
            return None
        durations[role] = float(value)
    return durations


def banked_roles(state: Mapping[str, Any]) -> tuple[str, ...]:
    """WHICH branches this round swept, read off its own banked gain plan.

    ``DRIVER_ROLES_BY_WAY`` resolved against the roles the plan names. Empty
    when the bank names no roles, or a set no speaker shape declares; every
    caller turns that into a refusal by name.
    """
    gains = state.get("gain_plan_db")
    banked = set(gains) if isinstance(gains, Mapping) else set()
    roles = DRIVER_ROLES_BY_WAY.get(len(banked), ())
    return roles if set(roles) == banked else ()


def round_bands_hz(
    state: Mapping[str, Any], overrides: Mapping[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    """The band each role THIS round swept, keyed by role.

    Roles come from :func:`banked_roles`, the same set
    :func:`rebuild_measure_program` composes against, so no caller can hand it
    a shape it will only refuse. Refuses rather than dropping a role it cannot
    place, which would compose a program silently missing a sweep.
    """
    roles = banked_roles(state)
    if not roles or not overrides.keys() >= set(roles):
        gains = state.get("gain_plan_db")
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {
                "missing": "a band for every role this round's gain plan names",
                "gain_plan_roles": sorted(gains) if isinstance(gains, Mapping) else [],
                "bands_offered": sorted(overrides),
            },
        )
    return {role: overrides[role] for role in roles}


def rebuild_measure_program(
    state: Mapping[str, Any], bands: Mapping[str, tuple[float, float]]
):
    """The round's MEASURE program, verified against its banked ``program_id``.

    Returns ``(program, downstream_gain_db, courtesy_prelude)``. Raises
    :class:`HarmonicEvidenceRefused` when no grid point reproduces the id — a
    reconstruction that cannot prove itself must not be read, because every
    harmonic offset derives from the sweep ``L`` this program carries.

    **Two unbanked parameters are solved here, not asserted:** the session
    volume, and the courtesy prelude, whose MEASURE value CHANGED when #2715
    replaced the flat ``COURTESY_PRELUDE_ENABLED`` global with the per-phase
    ``courtesy_prelude_for_phase`` (``True`` before it, ``False`` after). The
    prelude moves the program bytes, so a corpus banked either side of that
    commit reproduces under exactly one of the two values. The shipped rule is
    tried FIRST, and the search is safe in both directions because the
    ``program_id`` hash is what accepts it: a wrong prelude cannot match.

    **A third parameter — the duration fit (#2921) — is READ, never solved**,
    a fitted sweep's realized length being a continuous float no grid could
    reach. A round that banked it (:func:`_banked_sweep_durations_s`) is
    composed at EXACTLY that length; one that did not, or that banked
    something unusable, composes at nominal, and the refusal below keeps "did
    not bank" and "banked something unusable" apart.
    """
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )

    from .programs import (
        courtesy_prelude_for_phase,
        leading_pilot_role,
        pilot_gains,
    )

    roles = banked_roles(state)
    raw_gains = state.get("gain_plan_db")
    gains = raw_gains if isinstance(raw_gains, Mapping) else {}
    candidate = state.get("candidate") or {}
    want = candidate.get("program_id") if isinstance(candidate, Mapping) else None
    if not isinstance(want, str) or not want:
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {
                "missing": "candidate.program_id",
                "note": (
                    "the flow state records the id of the program that played; "
                    "without it a rebuilt program cannot be proved to be that "
                    "one, and an unproved program cannot be read for harmonics"
                ),
            },
        )
    if not roles or set(bands) != set(roles):
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {
                "missing": "a gain plan naming one shape's roles, and a band each",
                "gain_plan_roles": sorted(gains),
                "bands_supplied": sorted(bands),
                "program_id": want[:12],
            },
        )
    roles_bands = tuple(
        RoleBand(role, index, FrequencyBand(*bands[role]))
        for index, role in enumerate(roles)
    )
    # WHETHER the state carries a banking attempt at all, independent of whether
    # that attempt turned out usable: :func:`_banked_sweep_durations_s`
    # collapses "never banked" and "banked something unusable" to the same
    # ``None``, which is right for COMPOSING and wrong for the refusal note
    # below.
    raw_banked_durations = state.get("measure_sweep_durations_s")
    banked_durations_present = isinstance(raw_banked_durations, Mapping)
    banked_durations = _banked_sweep_durations_s(state, bands)
    shipped = courtesy_prelude_for_phase(PHASE_MEASURE)
    # Both pilot rules asked of the composer, never restated here.
    pilot_role = leading_pilot_role(roles_bands)
    for prelude in (shipped, not shipped):
        for downstream in _DOWNSTREAM_GRID_DB:
            program = build_measure_program(
                {role: float(gains[role]) for role in roles},
                roles_bands,
                sweep_durations=banked_durations,
                downstream_gain_db=float(downstream),
                leading_pilot_gains_db=pilot_gains(float(gains[pilot_role])),
                leading_pilot_role=pilot_role,
                courtesy_prelude=prelude,
            )
            if program.program_id == want:
                return program, float(downstream), bool(prelude)
    if not banked_durations_present:
        duration_cause = (
            "this round's sweeps were FITTED to a declared duration limit "
            "(#2921) — the composer shortens a sweep whose nominal length "
            "would overshoot that limit. This round did not bank the "
            "realized durations (#2923), so this replay composes at the "
            "nominal length and cannot match a fitted program"
        )
    elif banked_durations is None:
        duration_cause = (
            "this round's state carries a measure_sweep_durations_s entry, "
            "but the value for at least one role could not be used to "
            "compose a real sweep (malformed, non-positive, too short for "
            "one cycle at f1, or outside the measurable band) — TREATED "
            "THE SAME AS ABSENT, so this replay composes at the nominal "
            "length and cannot match a fitted program. This is a bank that "
            "exists but is not usable, not a round that never banked"
        )
    else:
        duration_cause = (
            "this round banked its realized sweep durations (#2923) and "
            "this replay composed at exactly them, so a duration fit is a "
            "LESS LIKELY cause here than it is for an unbanked round — "
            "check (1), (3), and (4) first — but it is not ruled out: a "
            "hand-edited banked value, or a genuine duration-limit change "
            "replayed against a byte-identical band, could still leave the "
            "banked figure wrong for this program"
        )
    raise HarmonicEvidenceRefused(
        PROGRAM_NOT_REPRODUCIBLE,
        {
            "program_id": want[:12],
            "bands_hz": {role: list(band) for role, band in sorted(bands.items())},
            "downstream_grid_db": [_DOWNSTREAM_GRID_DB[0], _DOWNSTREAM_GRID_DB[-1]],
            "downstream_grid_step_db": 0.5,
            # Two booleans, not one: "was anything banked" and "was what was banked
            # usable" are different facts (#2923 fix round 2). Absent from banking is
            # (False, False); banked-something-unusable is (True, False); banked and
            # used to compose every attempt above is (True, True) — reaching THIS
            # raise even so means the banked duration was fine and something else is
            # the actual cause.
            "measure_sweep_durations_banked": banked_durations_present,
            "measure_sweep_durations_usable": banked_durations is not None,
            "note": (
                "no session volume in the grid reproduces the banked program id "
                "with the courtesy prelude either on or off. Four causes, and "
                "the FIRST is the one an operator will hit on a real box: (1) "
                "the session volume is not on the solve grid — this round's "
                "volume is unbanked and brute-forced over half-dB steps from "
                "-40 dB, while the composer derives it as an unquantized float "
                "anywhere above the -60 dB floor, so a seat-level reference of "
                f"e.g. -24.7 dB is simply unreachable here; (2) {duration_cause}; "
                "(3) the driver bands supplied are wrong for this round; (4) "
                "this state does not describe a MEASURE round. Only (3) and "
                "(4) are fixable by re-invoking — (1) needs the round to bank "
                "its session volume, and (2) needs the round to bank its sweep "
                "durations (#2923) when it has not already"
            ),
        },
    )


def _state_capture_session_id(state: Mapping[str, Any]) -> str | None:
    """The flow state's own session id — a CAPTURE id, not the ring's bundle id.

    Two namespaces that look alike and are not: this is
    ``wired-dBetH8WEDOn8zCl5JMeAjQ``-shaped, while a ring sidecar stamps
    ``79679a65c207``, and nothing banked maps between them — which is why this
    is recorded for audit rather than compared for a refusal. See
    :func:`_scope_captures`.
    """
    value = state.get("session_id")
    return value if isinstance(value, str) and value else None


def _crossover_fc_hz(
    applied_profile: Mapping[str, Any] | None, profile_reason: str
) -> float:
    """The round's declared crossover corner, read from the applied-profile SSOT.

    ``analyze_program_capture`` REFUSES a MEASURE capture without one, and the
    fidelity gate runs that analysis, so this is a precondition rather than a
    nicety. Read from
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`,
    never from the flow state: what that state records about the previous
    apply is one apply behind after any v2 apply and arbitrarily behind after
    an apply through a door that never touches v2 state.
    """
    snapshot = (
        applied_profile.get("recomposition_snapshot")
        if isinstance(applied_profile, Mapping)
        else None
    )
    preset = snapshot.get("preset") if isinstance(snapshot, Mapping) else None
    regions = preset.get("crossover_regions") if isinstance(preset, Mapping) else None
    first = regions[0] if isinstance(regions, list) and regions else None
    value = first.get("fc_hz") if isinstance(first, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {
                "missing": (
                    "applied_baseline_profile.recomposition_snapshot.preset."
                    "crossover_regions[0].fc_hz"
                ),
                "reason": profile_reason,
                "note": (
                    "the shipped MEASURE analysis refuses a capture without the "
                    "round's crossover corner, and the fidelity gate runs that "
                    "analysis, so a round whose applied profile does not record "
                    "one cannot be read for harmonics at all"
                ),
            },
        )
    fc = float(value)
    if not math.isfinite(fc) or fc <= 0.0:
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {"field": "crossover_regions[0].fc_hz", "value": value},
        )
    return fc


def _bind_measure_captures(dumps_dir: Path) -> list[dict[str, Any]]:
    """Every MEASURE capture in the ring, bound to its sidecar by content.

    Found by :data:`~.evidence_packet.RING_SIDECAR_GLOB` and paired with the
    WAV in the sidecar's own sibling ``wav/`` — the same rule
    :func:`~.feature_classifier.load_round_captures` uses. Sidecars are
    deduplicated by ``wav_sha256``: several re-analyses of one capture are one
    capture, not several.

    **Every capture is returned, with the session identity it banked attached,
    and NOTHING is filtered here.** Scoping is :func:`_scope_captures`'s,
    because a binder that silently returned fewer rows would make that refusal
    impossible to reach.
    """
    seen_sha: set[str] = set()
    bound: list[dict[str, Any]] = []
    for sidecar_path in sorted(dumps_dir.glob(RING_SIDECAR_GLOB)):
        try:
            doc = json.loads(sidecar_path.read_text())
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(doc, Mapping) or doc.get("phase") != PHASE_MEASURE:
            continue
        sha = doc.get("wav_sha256")
        if not isinstance(sha, str) or sha in seen_sha:
            continue
        wav_path = sidecar_path.parent.parent / "wav" / f"{sidecar_path.stem}.wav"
        if not wav_path.is_file():
            continue
        identity = doc.get("jts_session_identity")
        banked = identity.get("session_id") if isinstance(identity, Mapping) else None
        seen_sha.add(sha)
        bound.append({
            "wav": wav_path,
            "sidecar": dict(doc),
            "wav_sha256": sha,
            "session_id": banked if isinstance(banked, str) and banked else None,
        })
    return bound


def _scope_captures(
    banked: list[dict[str, Any]], session_id: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The captures this round may be read from, and the scope that chose them.

    Returns ``(captures, scope)``. Raises :class:`HarmonicEvidenceRefused`
    rather than pooling captures whose provenance this reader cannot establish.

    **Why an unscoped ring is a refusal and not a default.** Every capture is
    read against ONE rebuilt program, and that program is where the published
    drive comes from — ``stimulus_peak_dbfs`` and ``effective_peak_dbfs`` are
    the SEGMENT's, not the capture's. Measured on the shipped corpus, one
    capture read against a neighbouring session's program reported
    ``-6.0 / -26.0`` where its own program says ``-10.2 / -30.2`` — **4.2 dB**,
    on exactly the field this module says must never be published apart from
    the ratio. Neither other gate can see it: the program-identity gate says
    nothing about which captures were played through the program, and every
    :data:`FIDELITY_FIELDS` entry is amplitude-invariant. So a supplied
    ``session_id`` scopes to it, and an absent one is admitted ONLY when the
    ring's MEASURE captures all carry one readable identity.

    **What this does NOT guard, stated because it is the only unguarded seam
    left that can publish a wrong number.** The ``state`` is TRUSTED to belong
    to the scope: nothing here checks that the flow state the program was
    rebuilt from describes the same round the ``session_id`` selects, so a
    caller passing a scope from one round and a state from another
    mis-attributes drive through every gate cleanly — 5/5 fidelity, zero
    refusals, an authoritative-looking ``captures.scope`` block. It is not
    guardable today: the state's ``session_id`` is a CAPTURE id while the ring
    stamps the BUNDLE id, nothing banked maps between them, and the capture's
    ``provenance`` block is empty on every sidecar in both shipped corpora.
    **What retires this is the capture banking its own program_id.** Until
    then the capture id is at least RECORDED in the artifact's ``program``
    block, so a mis-scoped read is auditable after the fact.
    """
    if session_id is not None:
        return (
            [capture for capture in banked if capture["session_id"] == session_id],
            {
                "session_id": session_id,
                "source": "supplied by the caller (the bundle's info.json)",
                "n_ring_captures": len(banked),
            },
        )
    identities = {capture["session_id"] for capture in banked}
    if len(identities) > 1 or (identities and None in identities):
        raise HarmonicEvidenceRefused(
            RING_NOT_SCOPED_TO_ONE_SESSION,
            {
                "n_ring_captures": len(banked),
                "distinct_session_ids": sorted(
                    identity for identity in identities if identity is not None
                ),
                "n_unattributed": sum(
                    1 for capture in banked if capture["session_id"] is None
                ),
                "note": (
                    "no scope was supplied and this ring's MEASURE captures do "
                    "not all belong to one session, so which of them this "
                    "round's program was played through cannot be established. "
                    "Reading them together would publish one round's drive "
                    "level against another round's capture, which no gate here "
                    "can detect because the fidelity fields are all "
                    "amplitude-invariant. Supply the bundle's session_id"
                ),
            },
        )
    only = next(iter(identities), None)
    return (
        list(banked),
        {
            "session_id": only,
            "source": (
                "no scope supplied; the ring's MEASURE captures all carry this "
                "one session identity"
            ),
            "n_ring_captures": len(banked),
        },
    )


def _sign_convention(calibration_id: str) -> str:
    """How to read this session's calibration file — asked of the product's own
    registry rather than pinned here.

    A vendor file states either the microphone's RESPONSE or a CORRECTION, and
    the two differ by a sign. Getting it backwards moves every magnitude in
    the read without moving one timing diagnostic, so nothing downstream would
    catch it — which is why the convention is resolved from the id the SESSION
    banked rather than from a default or a flag.
    """
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        SUPPORTED_MODELS,
    )

    parts = set(str(calibration_id).split("-"))
    for key, spec in SUPPORTED_MODELS.items():
        if key in parts:
            return str(spec["sign_convention"])
    return DEFAULT_SIGN_CONVENTION


def _calibration_for(captures: list[dict[str, Any]], text: str | None):
    """``(curve, description)`` for this round's captures, or ``(None, why)``.

    The convention comes from the FIRST bound capture's own
    ``setup_calibration_id``, that being the microphone the session recorded
    using; a file parsed under the other convention would be applied with its
    sign flipped.
    """
    if text is None:
        return None, {
            "applied": False,
            "note": (
                "no calibration supplied: every harmonic-to-fundamental ratio "
                "carries the microphone's own response across an octave"
            ),
        }
    from jasper.audio_measurement.calibration import parse_calibration_text

    calibration_id = str(captures[0]["sidecar"].get("setup_calibration_id") or "")
    convention = _sign_convention(calibration_id)
    curve = parse_calibration_text(text, sign_convention=convention)
    return curve, {
        "applied": True,
        "sign_convention": convention,
        "setup_calibration_id": calibration_id,
        "n_points": len(curve.freqs_hz),
    }


def _fidelity_failures(
    replayed: Mapping[str, Any], banked: Mapping[str, Any]
) -> list[str]:
    """Which :data:`FIDELITY_FIELDS` the replay failed to reproduce.

    A field the sidecar does not record is not compared — the bank predates some
    of them — but a field it records and the replay omits IS a failure, because
    that is the reconstruction losing something the session had.
    """
    failures: list[str] = []
    for field in FIDELITY_FIELDS:
        if field not in banked or banked[field] is None:
            continue
        mine, theirs = replayed.get(field), banked[field]
        if mine is None:
            failures.append(f"{field}: replay produced nothing, banked {theirs!r}")
        elif isinstance(theirs, bool) or isinstance(mine, bool):
            if bool(mine) != bool(theirs):
                failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
        elif isinstance(theirs, (int, float)) and isinstance(mine, (int, float)):
            if abs(float(mine) - float(theirs)) > FIDELITY_TOLERANCE:
                failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
        elif mine != theirs:
            failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
    return failures


def _glitch_disclosure(
    replayed: Mapping[str, Any], banked: Mapping[str, Any]
) -> str | None:
    """A note when the bank and the replay disagree about capture integrity.

    Not a gate (see :data:`FIDELITY_FIELDS`): a capture the session REJECTED
    and this read treats as clean is being included on the strength of D7's
    re-derivation, and the reader should be told which ones those are.
    """
    theirs, mine = banked.get("glitch_detected"), replayed.get("glitch_detected")
    if theirs is None or mine is None or bool(theirs) == bool(mine):
        return None
    if bool(theirs):
        return (
            "banked glitch_detected=true, replay says false — rejected by the "
            "pre-D7 desync guard, read here on D7's re-derivation"
        )
    return "banked glitch_detected=false, replay says TRUE — read with suspicion"


def _read_one_capture(program, samples, sidecar, *, orders, calibration, fc_hz):
    """Gate one capture, then read every sweep segment's distortion.

    Returns ``(readings, failures, disclosure, compared)``. ``readings`` is
    empty when a gate failed — a capture whose analysis does not reproduce is
    not evidence about a speaker — and a sidecar carrying NONE of the gate
    fields is refused outright: zero comparisons is not a passed gate.
    """
    from jasper.audio_measurement import deconv
    from jasper.audio_measurement.distortion import read_segment_distortion
    from jasper.audio_measurement.program import KIND_SWEEP
    from jasper.audio_measurement.program_analysis import (
        CAPTURE_BOUND_MARGIN_S,
        MeasurementGeometry,
        MeasurementPriors,
        _estimate_drift,
        _global_offset,
        _locate_segments,
        analysis_diagnostic_summary,
        analyze_program_capture,
    )

    banked = sidecar.get("diagnostic") or {}
    compared = sum(
        1 for field in FIDELITY_FIELDS
        if field in banked and banked[field] is not None
    )
    if compared == 0:
        return [], [
            "sidecar carries none of the gate's diagnostic fields — nothing to "
            "compare means nothing was validated, so the capture is refused "
            "rather than read ungated"
        ], None, 0

    rate = program.sample_rate_hz
    analysis = analyze_program_capture(
        program, samples, rate,
        calibration=calibration,
        geometry=MeasurementGeometry(),
        priors=MeasurementPriors(crossover_fc_hz=fc_hz),
    )
    replayed = analysis_diagnostic_summary(analysis)
    failures = _fidelity_failures(replayed, banked)
    if failures:
        return [], failures, None, compared
    disclosure = _glitch_disclosure(replayed, banked)

    # The same bounding `analyze_program_capture` applies before locating, so
    # the anchors below are the anchors it used.
    bounded = deconv.cap_capture_length(
        samples,
        sweep_len=program.total_samples,
        sample_rate=rate,
        max_capture_seconds=program.total_samples / rate + CAPTURE_BOUND_MARGIN_S,
    )
    global_offset, _first, stimuli, _ambiguous = _global_offset(program, bounded, rate)
    locations = _locate_segments(program, bounded, rate, global_offset, stimuli)
    epsilon = _estimate_drift(program, bounded, rate, locations).epsilon_ppm / 1e6

    readings = []
    for segment in program.stimulus_segments():
        if segment.kind != KIND_SWEEP:
            continue
        readings.append(
            read_segment_distortion(
                program, bounded, segment.segment_id,
                global_offset + segment.start_sample,
                orders=orders, calibration=calibration, epsilon=epsilon,
                level_notes={
                    "wav_sha256_12": str(sidecar.get("wav_sha256", ""))[:12],
                    "phase": sidecar.get("phase"),
                },
            )
        )
    return readings, [], disclosure, compared


def _median(values: Sequence[float]) -> float:
    """Median, or NaN over nothing — never a zero standing in for no data."""
    real = [value for value in values if math.isfinite(value)]
    return statistics.median(real) if real else float("nan")


def _spread(values: Sequence[float]) -> float | None:
    """Sample standard deviation across in-capture repeats, or ``None``.

    ``None`` below two real values rather than 0.0, on the cross-seat block's
    rule: a sample standard deviation is UNDEFINED at n=1 and a zero would say
    the repeats agreed. ``statistics.stdev`` RAISES at n < 2 instead of
    returning a silent NaN, and the ``len < 2`` guard stands in front of it.
    """
    real = [value for value in values if math.isfinite(value)]
    if len(real) < 2:
        return None
    try:
        return round(statistics.stdev(real), _DB_DECIMALS)
    except OverflowError:
        return None


def _nullable(value: float, decimals: int = _DB_DECIMALS) -> float | None:
    """One rounded number, or ``None`` where the reading is not real.

    NaN means "past this order's own band edge" and reaches JSON as ``null`` —
    never as a number, because a very negative float would read as a
    preternaturally clean driver exactly where nothing was measured.
    """
    return round(float(value), decimals) if math.isfinite(value) else None


def _role_block(role: str, readings: list, sha12: str, orders: tuple[int, ...]) -> dict:
    """One (capture, role)'s rows, pooled over that role's in-capture sweeps.

    **Pooling is per capture on purpose, and it is what makes the spread below
    one kind.** A MEASURE capture is one pose, so the sweeps of one role
    inside it are that pose's repeats and their scatter is the RANDOM
    repeatability term — the statistic
    ``linearization_envelope.compute_sigma_curve`` owns. Pooling across
    CAPTURES would mix it with whatever differs between takes, which is the
    unseparated case, so a round with two MEASURE captures publishes two
    blocks. Pooling below is BY GRID INDEX, valid because every sweep of one
    role shares one ``SweepMeta`` — asserted rather than assumed, because
    pooling by index lies silently otherwise.
    """
    first = readings[0]
    if not all(np.array_equal(r.freqs_hz, first.freqs_hz) for r in readings):
        raise ValueError(f"role {role}: sweep grids disagree; cannot pool by index")

    fund_pool = np.median(np.stack([r.fundamental_db for r in readings]), axis=0)
    fund_delta = fund_pool - float(np.median(fund_pool))

    rows: list[dict[str, Any]] = []
    for probe in PROBE_FREQUENCIES_HZ:
        if not (first.band_hz[0] <= probe <= first.band_hz[1]):
            continue
        index = int(np.argmin(np.abs(first.freqs_hz - probe)))
        row: dict[str, Any] = {
            "hz": probe,
            "fundamental_re_band_median_db": _nullable(fund_delta[index]),
        }
        for order in orders:
            values, floors, limited = [], [], []
            for reading in readings:
                at = int(np.argmin(np.abs(reading.freqs_hz - probe)))
                values.append(float(reading.relative_db[order][at]))
                floors.append(float(reading.floor_relative_db[order][at]))
                limited.append(bool(reading.floor_limited(order)[at]))
            row[f"h{order}_below_fundamental_db"] = _nullable(_median(values))
            row[f"h{order}_floor_below_fundamental_db"] = _nullable(_median(floors))
            # Majority vote, so one sweep's noise spike cannot flag a point the
            # others read as clear — the same rule the summary below pools by.
            row[f"h{order}_floor_limited"] = (
                None if not math.isfinite(_median(values))
                else sum(limited) > len(limited) / 2
            )
            row[f"h{order}_repeat_spread_db"] = _spread(values)
        row["thd_percent"] = _nullable(
            _median([
                float(r.thd_percent[int(np.argmin(np.abs(r.freqs_hz - probe)))])
                for r in readings
            ]),
            _PERCENT_DECIMALS,
        )
        rows.append(row)

    worst: dict[str, Any] = {}
    floor_fraction: dict[str, float] = {}
    from jasper.audio_measurement.distortion import worst_clear_of_floor

    for order in orders:
        pooled = np.median(
            np.stack([r.relative_db[order] for r in readings]), axis=0
        )
        limited_mask = np.stack(
            [r.floor_limited(order) for r in readings]
        ).sum(axis=0) > len(readings) / 2
        floor_fraction[f"h{order}"] = round(float(np.mean(limited_mask)), 3)
        hz, value = worst_clear_of_floor(first.freqs_hz, pooled, limited_mask)
        worst[f"h{order}"] = (
            None if not math.isfinite(value)
            else {"hz": round(float(hz), 1), "below_fundamental_db": round(value, 1)}
        )

    drives = [r.drive for r in readings]
    return {
        "role": role,
        "wav_sha256_12": sha12,
        "n_sweeps": len(readings),
        "sweep": {
            "f1_hz": round(float(first.sweep.f1), 1),
            "f2_hz": round(float(first.sweep.f2), 1),
            "L_s": round(float(first.sweep.L), 4),
            "read_band_hz": [
                round(float(first.band_hz[0]), 1),
                round(float(first.band_hz[1]), 1),
            ],
        },
        "drive": {
            "stimulus_peak_dbfs": _nullable(
                _median([d.stimulus_peak_dbfs for d in drives]), 2
            ),
            "effective_peak_dbfs": _nullable(
                _median([d.effective_peak_dbfs for d in drives]), 2
            ),
            "capture_peak_dbfs": _nullable(
                _median([d.capture_peak_dbfs for d in drives]), 2
            ),
            "capture_rms_dbfs": _nullable(
                _median([d.capture_rms_dbfs for d in drives]), 2
            ),
        },
        "images_clean": all(r.images_clean for r in readings),
        "worst_clearance_s": round(min(r.clearance_s for r in readings), 3),
        "worst": worst,
        "floor_limited_fraction": floor_fraction,
        "rows": rows,
    }


def read_round_harmonics(
    round_dir: Path,
    dumps_dir: Path,
    state: Mapping[str, Any],
    bands: Mapping[str, tuple[float, float]],
    *,
    session_id: str | None = None,
    orders: Sequence[int] = HARMONIC_ORDERS,
    calibration_text: str | None = None,
    applied_profile_path: Path | None = None,
) -> dict[str, Any]:
    """One round's H2/H3 reading, as the document the packet carries.

    ``round_dir`` names the reading in the artifact, ``dumps_dir`` is the ring
    root, ``state`` is the flow state's parsed contents (its ``gain_plan_db``
    and ``candidate.program_id`` are what the MEASURE program is rebuilt from
    and proved against), and ``bands`` maps each driver role to its
    ``(f1, f2)``.

    ``applied_profile_path`` is the applied-baseline-profile SSOT, where the
    round's crossover corner is read from (:func:`_crossover_fc_hz`). Its
    absence, or an unreadable file, is a refusal rather than a fallback to
    what the flow state records about a previous apply.

    ``session_id`` is the BUNDLE session id. Omitting it is NOT "read
    everything": an unscoped ring whose captures do not all carry one
    identity is REFUSED by name — see :func:`_scope_captures` for the
    measured size of that error. The scope that was applied rides in the
    artifact.

    ``calibration_text`` is a vendor calibration file's CONTENTS, not a
    parsed curve: the sign convention it must be read under depends on which
    microphone this round's own captures recorded through.

    Raises :class:`HarmonicEvidenceRefused` rather than returning a partial
    document. Every capture that failed a gate is nonetheless COUNTED and its
    failures published, because a round where three of four captures failed
    fidelity is a different round from one where all four passed.
    """
    orders = tuple(int(order) for order in orders)
    applied_profile, profile_reason = _applied_profile_source(applied_profile_path)
    fc_hz = _crossover_fc_hz(applied_profile, profile_reason)
    program, downstream_db, prelude = rebuild_measure_program(state, bands)
    captures, scope = _scope_captures(
        _bind_measure_captures(dumps_dir), session_id
    )
    if not captures:
        raise HarmonicEvidenceRefused(
            NO_ADMISSIBLE_CAPTURES,
            {
                "phase": PHASE_MEASURE,
                "dumps_dir": dumps_dir.name,
                "scope": scope,
                "note": (
                    "harmonics are read from the per-driver MEASURE program, "
                    "whose sweeps are one driver at a time; a summed VERIFY "
                    "capture cannot attribute a harmonic to a driver. The scope "
                    "above says what was looked for: a ring holding captures "
                    "only from OTHER sessions lands here too"
                ),
            },
        )

    calibration, calibration_note = _calibration_for(captures, calibration_text)

    blocks: list[dict[str, Any]] = []
    read: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    disclosures: list[dict[str, str]] = []
    for capture in captures:
        sha12 = capture["wav_sha256"][:12]
        try:
            samples = _read_mono(capture["wav"])
        except (OSError, wave.Error, ValueError) as exc:
            refused.append({
                "wav_sha256_12": sha12,
                "fidelity_fields_compared": 0,
                "failures": [str(exc)],
            })
            continue
        readings, failures, disclosure, compared = _read_one_capture(
            program, samples, capture["sidecar"],
            orders=orders, calibration=calibration, fc_hz=fc_hz,
        )
        if failures:
            refused.append({
                "wav_sha256_12": sha12,
                "fidelity_fields_compared": compared,
                "failures": failures,
            })
            continue
        # The count rides on a PASS too, not only on a refusal: a capture whose
        # sidecar carried one of the five gate fields passed a much weaker gate
        # than one that carried all five.
        read.append({
            "wav_sha256_12": sha12,
            "fidelity_fields_compared": compared,
        })
        if disclosure:
            disclosures.append({"wav_sha256_12": sha12, "note": disclosure})
        by_role: dict[str, list] = {}
        for reading in readings:
            by_role.setdefault(reading.role or "?", []).append(reading)
        for role, role_readings in sorted(by_role.items()):
            blocks.append(_role_block(role, role_readings, sha12, orders))

    if not blocks:
        raise HarmonicEvidenceRefused(
            NO_CAPTURE_PASSED_THE_GATES,
            {
                "n_captures": len(captures),
                "refused": refused,
                "note": (
                    "every MEASURE capture in the ring failed a gate, so no "
                    "reading is reportable. A capture whose analysis does not "
                    "reproduce is not evidence about a speaker"
                ),
            },
        )

    return {
        "artifact_kind": HARMONICS_ARTIFACT_KIND,
        "artifact_schema_version": HARMONICS_SCHEMA_VERSION,
        "round_dir": round_dir.name,
        "orders": list(orders),
        "program": {
            "program_id": program.program_id,
            "solved_downstream_gain_db": downstream_db,
            "solved_courtesy_prelude": prelude,
            "crossover_fc_hz": round(fc_hz, 1),
            # WHICH round's state this program was rebuilt from, in the state's own
            # namespace. It cannot be compared against `captures.scope` (that is a
            # BUNDLE id, this is a CAPTURE id, and nothing banked maps between them),
            # so it guards nothing at read time — it is here so the one seam
            # `_scope_captures` cannot refuse is at least AUDITABLE afterwards.
            "state_capture_session_id": _state_capture_session_id(state),
        },
        "captures": {
            # WHICH captures this reading is of, and by what rule they were chosen.
            # Published because the drive levels below come from the rebuilt program
            # rather than from each capture.
            "scope": scope,
            "n_read": len({block["wav_sha256_12"] for block in blocks}),
            "n_refused": len(refused),
            "read": read,
            "refused": refused,
            "integrity_disclosures": disclosures,
            "fidelity_fields": list(FIDELITY_FIELDS),
        },
        "calibration": calibration_note,
        "roles": blocks,
    }


def read_bundle_harmonics(
    bundle_dir: Path,
    dumps_dir: Path,
    state_path: Path,
    band_overrides: Mapping[str, tuple[float, float]],
    *,
    calibration_path: Path | None = None,
    applied_profile_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """One bundle's round (``info.json`` beside ``evidence/v1/artifacts/``),
    resolved by :func:`~.evidence_packet.round_artifact_dir` so the reading
    lands where the packet reader looks. Raises ``OSError``/``ValueError`` for
    what cannot be read, :class:`HarmonicEvidenceRefused` on a decline.
    """
    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        if why == NO_ROUND_ARTIFACTS_REASON:
            why += (
                " — the bundle must hold info.json beside "
                "evidence/v1/artifacts/crossover_v2/<capture-session-id>/"
            )
        raise ValueError(why)

    info = json.loads((bundle_dir / "info.json").read_text())
    state = json.loads(state_path.read_text())
    if not isinstance(state, dict):
        raise ValueError(f"the flow state at {state_path} is not a JSON object")
    session_id = info.get("session_id") if isinstance(info, dict) else None

    artifact = read_round_harmonics(
        round_dir,
        dumps_dir,
        state,
        round_bands_hz(state, band_overrides),
        session_id=session_id if isinstance(session_id, str) else None,
        # The file's CONTENTS, not a parsed curve: the sign convention it must
        # be read under comes from the microphone this round's own captures
        # recorded through, which only the instrument can see.
        calibration_text=(
            calibration_path.read_text() if calibration_path is not None else None
        ),
        applied_profile_path=applied_profile_path,
    )
    return round_dir, artifact
