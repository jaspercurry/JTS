# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the reflection gate actually did — priced, banded, and said out loud.

Pipeline stage: **diagnose**. :mod:`~jasper.audio_measurement.gating` owns
the gate (detection, window, floors, the classification ledger); this module
owns the *proof* that the gate helped, and the one sentence a reader gets
about it. The split is not cosmetic — pricing a gate requires knowing where
the DUT actually radiates, which is program knowledge the gating stage does
not and should not have.

It owns three things:

* :func:`evaluation_band_hz` — the band a gated capture can be read over,
  from the caller's floor. This is the module's band policy and its single
  owner.
* :func:`pre_post_gate_delta` — how much the gate moved the spectrum's
  SHAPE, over that band.
* :class:`GateDisclosure` / :func:`build_gate_disclosure` — the typed
  record, with :func:`describe_gate` rendering it as copy.

It deliberately does NOT own: whether a capture gates, where a window ends,
what refuses, or any floor's value. Every one of those is
:mod:`~jasper.audio_measurement.gating`'s, and nothing here writes back into
a gating decision.

Why the evaluation band is an intersection (E5, issue #1969)
------------------------------------------------------------

The v1 contract said to price the gate over ``[2.5/T, 20 kHz]``. Read
literally that is wrong for a per-branch capture, and the banked measurement
says by how much: a tweeter high-passed at 2 kHz was evaluated from 357 Hz,
where it has no output, so the observable compared one noise floor against
another and reported **RMS 4.045 dB** where the honest number over the band
the branch actually radiates is **1.368 dB** — 3x the RMS and 4.4x the max
(``captures/gating-experiments-20260731/`` §5). Both numbers are recorded
here, so the correction stays auditable rather than merely applied.

Why a delta near zero is not self-explanatory
---------------------------------------------

The same experiment killed the intuitive reading. "Delta ~ 0 means the gate
sat at a bound" is false: a 7 ms gate always truncates the room tail, so the
delta is never zero — the tweeter's bound-sitting gate still moved the
spectrum 1.37 dB RMS while the reflection-bound woofer moved 2.59 dB. The
discriminator is the delta **together with** ``floor_source``:

* small delta + :data:`~jasper.audio_measurement.gating.FLOOR_MEASURED` —
  a reflection was found and removing it barely changed anything. The
  capture is genuinely clean.
* small delta + :data:`~jasper.audio_measurement.gating.FLOOR_SEARCH_BOUND`
  — nothing was found and the window was capped. The gate did essentially
  nothing, and **nothing was proven** about reflections.

Those are opposite epistemic states and they print the same number, which is
why :func:`describe_gate` never renders one without the other.

Two floors, not one (#3495)
---------------------------

Beside the window's floors this module publishes the ROOM's, with its
provenance — the identity is
:func:`~jasper.audio_measurement.gating.f_entanglement_floor_hz`'s and what
a reader does with it is `docs/tuning-methodology.md`'s. On the rig class
whose first bounce lands while the direct sound is still decaying the
reflection finder structurally never fires (#3502), so the source published
here is routinely
:data:`~jasper.audio_measurement.gating.ENTANGLEMENT_SOURCE_UNKNOWN`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.audio_measurement import gating
from jasper.json_fields import finite_float as _finite

#: Transform length for the pre/post-gate magnitude comparison, and the
#: ceiling of the "literal" (un-intersected) evaluation band. Both are the
#: banked E5 record's (``captures/gating-experiments-20260731/kit/
#: gate_proof.py``) so this module and that artifact produce the same
#: numbers from the same impulse response.
DELTA_NFFT = 1 << 16
DELTA_CEILING_HZ = 20000.0

#: Below this, ``describe_gate`` calls the gate's spectral effect "small".
#: A rendering threshold for one adjective, not a decision boundary —
#: nothing branches on it but the wording, and the number itself is always
#: printed beside the word.
SMALL_DELTA_RMS_DB = 1.0


def evaluation_band_hz(
    floor_hz: float | None,
    radiated_band_hz: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """The band a gated capture can be read over, in Hz.

    ``[max(floor_hz, radiated_lo), radiated_hi]`` — the CALLER'S floor
    intersected with the band the DUT actually radiates. Which floor is the
    caller's: :func:`pre_post_gate_delta` prices the gate over the trusted
    floor (``2.5/T``), while
    :func:`jasper.audio_measurement.comparison_bands.crossover_region_band_hz`
    grades over the validity floor (``1/T``), the trusted floor bounding only
    the flat spec's own bands
    (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`).
    An empty intersection or an unusable input yields ``None``, itself the
    finding: no band, so no number invented for one.

    ``radiated_band_hz`` is the caller's; this module does not guess it.
    Falling back to :data:`DELTA_CEILING_HZ` when it is absent would
    reproduce exactly the over-report E5 measured, so an absent band yields
    ``None`` rather than a default.
    """
    if radiated_band_hz is None or floor_hz is None:
        return None
    lo_r, hi_r = float(radiated_band_hz[0]), float(radiated_band_hz[1])
    floor = float(floor_hz)
    if not all(math.isfinite(v) for v in (lo_r, hi_r, floor)):
        return None
    return gating.intersect_bands((floor, hi_r), (lo_r, hi_r))


def _magnitude_db(ir: np.ndarray, sample_rate: float, n_fft: int) -> tuple[np.ndarray, np.ndarray]:
    """``(freqs_hz, magnitude_db)`` of ``ir`` on a fixed transform.

    Uncalibrated on purpose: the microphone calibration is the same
    multiplicative curve on both sides of a pre/post-gate difference, so it
    cancels exactly and applying it would only cost a multiply.
    """
    spectrum = np.fft.rfft(np.asarray(ir, dtype=np.float64), n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    return freqs, 20.0 * np.log10(np.maximum(np.abs(spectrum), 1e-12))


def _band_delta(
    freqs: np.ndarray,
    gated_db: np.ndarray,
    ungated_db: np.ndarray,
    lo: float,
    hi: float,
) -> tuple[float | None, float | None, int]:
    """Mean-removed ``(rms_db, max_db, n_bins)`` of ``gated - ungated``.

    The mean is removed because a gate necessarily removes energy, so a
    constant offset between the two curves is expected and says nothing
    about whether the gate changed the response. What the observable is
    asking is whether the gate changed the spectrum's SHAPE — the same
    offset-vs-shape discipline the frame work applies to cross-curve
    comparisons generally.
    """
    mask = (
        (freqs >= lo) & (freqs <= hi) & np.isfinite(gated_db) & np.isfinite(ungated_db)
    )
    n_bins = int(np.count_nonzero(mask))
    if n_bins == 0:
        return None, None, 0
    d = gated_db[mask] - ungated_db[mask]
    d = d - d.mean()
    return float(np.sqrt(np.mean(d**2))), float(np.max(np.abs(d))), n_bins


def pre_post_gate_delta(
    ir: np.ndarray,
    gated_ir: np.ndarray,
    sample_rate: int,
    *,
    trusted_floor_hz: float | None,
    radiated_band_hz: tuple[float, float] | None,
    n_fft: int = DELTA_NFFT,
) -> dict[str, Any] | None:
    """Price what the gate did to the spectrum. Reporting only.

    Returns the delta block merged into a gating block, or ``None`` when
    the inputs cannot support one (degenerate rate, mismatched lengths, no
    usable evaluation band). Both readings are reported:

    ``rms_db`` / ``max_db`` over ``eval_band_hz``
        The honest numbers, over :func:`evaluation_band_hz`.
    ``literal_rms_db`` / ``literal_max_db`` over ``literal_band_hz``
        The v1 contract's un-intersected ``[2.5/T, 20 kHz]`` reading, kept
        so the size of the correction stays visible in the record rather
        than only in this docstring.

    Units are dB throughout. ``n_bins`` is the count of FFT bins the honest
    reading averaged; it is the tell for a band too narrow to mean much.

    ``n_fft`` is FIXED, not derived from the IR length. Two reasons, and one
    consequence worth stating plainly:

    * records from captures of different lengths stay comparable, which a
      length-derived transform would quietly break;
    * the work is bounded regardless of what is handed in — this runs on
      the Pi during commissioning.

    The consequence is that an IR longer than ``n_fft`` is analysed over its
    first ``n_fft`` samples (1.365 s at 48 kHz). That is ample for a
    gate-effect comparison, whose content is in the first few milliseconds,
    and it does not arise on the production path at all: the analysed IR is
    already arrival-windowed to ~65 ms before it reaches the gate. It is
    also exactly what the banked E5 record did, so this function and that
    artifact return the same numbers from the same impulse response.
    """
    ir_arr = np.asarray(ir, dtype=np.float64)
    gated_arr = np.asarray(gated_ir, dtype=np.float64)
    sr = float(sample_rate)
    if (
        ir_arr.ndim != 1
        or gated_arr.shape != ir_arr.shape
        or ir_arr.size == 0
        or sr <= 0
        or not math.isfinite(sr)
    ):
        return None
    band = evaluation_band_hz(trusted_floor_hz, radiated_band_hz)
    # A band implies both inputs were usable — that is the ONE guard, so the
    # locals below need no second round of None-checking.
    if band is None or radiated_band_hz is None or trusted_floor_hz is None:
        return None

    size = int(n_fft)
    if size < 2:
        return None
    freqs, gated_db = _magnitude_db(gated_arr, sr, size)
    _freqs, ungated_db = _magnitude_db(ir_arr, sr, size)

    lo, hi = band
    rms, peak, n_bins = _band_delta(freqs, gated_db, ungated_db, lo, hi)
    lit_rms, lit_peak, _lit_bins = _band_delta(
        freqs, gated_db, ungated_db, float(trusted_floor_hz), DELTA_CEILING_HZ
    )
    return {
        "radiated_band_hz": [float(radiated_band_hz[0]), float(radiated_band_hz[1])],
        "eval_band_hz": [lo, hi],
        "rms_db": rms,
        "max_db": peak,
        "n_bins": n_bins,
        "literal_band_hz": [float(trusted_floor_hz), DELTA_CEILING_HZ],
        "literal_rms_db": lit_rms,
        "literal_max_db": lit_peak,
    }


@dataclass(frozen=True)
class GateDisclosure:
    """Everything a reader needs to judge one capture's gate. Typed, read-only.

    Built by :func:`build_gate_disclosure` from a persisted gating block —
    that function is the single writer, and this class holds no facts of its
    own beyond what the block already carries. Nothing here participates in
    a gating decision; it exists so a record cannot report a window without
    reporting why the window is that long and what it bought.

    Units: ``gate_ms`` milliseconds; ``f_min_hz`` / ``f_trusted_hz`` /
    ``entanglement_floor_hz`` hertz; ``delta_rms_db`` decibels.
    ``source_of_bound`` carries
    :mod:`~jasper.audio_measurement.gating`'s vocabulary verbatim
    (``measured_reflection`` / ``search_span_bound`` / ``None``) — the
    gating contract's ``source_of_bound`` and that module's ``floor_source``
    are one field under two names, so only one of them is ever persisted.
    See :mod:`~jasper.audio_measurement.gating`'s "The gating contract".
    """

    gate_ms: float | None
    source_of_bound: str | None
    exempt_reason: str | None
    f_min_hz: float | None
    f_trusted_hz: float | None
    direct_peak_ms: float | None
    reflection_toa_ms: float | None
    delta_rms_db: float | None
    delta_band_hz: tuple[float, float] | None
    #: Ledger entries classified
    #: :data:`~jasper.audio_measurement.gating.CLASS_DUT_INTERNAL` ONLY — a
    #: ``gateable`` candidate the detector simply did not select is not a
    #: loudspeaker-internal feature and must not be counted as one.
    internal_reflection_count: int
    #: Every ledger entry, internal or not. Beside the count above so a
    #: reader can see that some candidates were classified the other way.
    ledger_count: int
    #: The room's floor in Hz —
    #: :func:`~jasper.audio_measurement.gating.f_entanglement_floor_hz`.
    #: ``None`` is unknown.
    entanglement_floor_hz: float | None = None
    #: Which of :mod:`~jasper.audio_measurement.gating`'s three
    #: ``ENTANGLEMENT_SOURCE_*`` words the floor came from.
    #: A declared geometry never masquerades as a measurement (#3502).
    #: The two fields are one
    #: :class:`~jasper.audio_measurement.gating.EntanglementFloor`, flattened
    #: for the readers that take a single field; that type holds the rule
    #: binding them.
    entanglement_floor_source: str = gating.ENTANGLEMENT_SOURCE_UNKNOWN

    @property
    def gated_anything(self) -> bool:
        """True only when a reflection was measured and the window stops at it.

        The claim "reflections were removed" is true here and NOWHERE else.
        A window capped at the search ceiling gated nothing out, however
        long it is.
        """
        return self.source_of_bound == gating.FLOOR_MEASURED

    @property
    def reflection_delay_ms(self) -> float | None:
        """Reflection arrival RELATIVE to the direct one, in milliseconds.

        Both stored times are absolute within the analysed IR (an artifact
        of the deconvolution window's origin), which is meaningless to a
        reader; the delay between them is the physical quantity. ``None``
        when either side is unknown.
        """
        return _relative_delay_ms(self.reflection_toa_ms, self.direct_peak_ms)


def _relative_delay_ms(
    reflection_toa_ms: float | None, direct_peak_ms: float | None
) -> float | None:
    if reflection_toa_ms is None or direct_peak_ms is None:
        return None
    return reflection_toa_ms - direct_peak_ms


def _entanglement_floor(
    source_of_bound: str | None,
    reflection_delay_ms: float | None,
    declared_first_bounce_s: float | None,
    *,
    schema_version: Any,
) -> gating.EntanglementFloor:
    """The room's floor — measured beats declared beats unknown.

    A measured reflection is the only source that also proves the bounce it
    times exists; a declared geometry is the operator's tape measure. Neither
    available is where this rig class lives (#3502). A block whose schema
    predates :data:`~jasper.audio_measurement.gating.ARRIVAL_REPORTED_SINCE_SCHEMA_VERSION`
    reports no arrival and has nothing to time, so it falls through to
    declared however its window was bound.

    The formula and the floor/provenance invariant are
    :class:`~jasper.audio_measurement.gating.EntanglementFloor`'s; what this
    adds is the reader's contract — a missing or non-physical bounce time is
    UNKNOWN here, not an exception, since this runs on records written by
    builds that never wrote the field.
    """
    version = _finite(schema_version)
    reports_arrival = (
        version is None or version >= gating.ARRIVAL_REPORTED_SINCE_SCHEMA_VERSION
    )
    measured_s = _finite(
        reflection_delay_ms / 1000.0 if reflection_delay_ms is not None else None
    )
    if (
        reports_arrival
        and source_of_bound == gating.FLOOR_MEASURED
        and measured_s is not None
    ):
        try:
            return gating.EntanglementFloor.from_bounce_s(
                measured_s, gating.ENTANGLEMENT_SOURCE_MEASURED
            )
        except (TypeError, ValueError):
            pass
    declared_s = _finite(declared_first_bounce_s)
    if declared_s is not None:
        try:
            return gating.EntanglementFloor.from_bounce_s(
                declared_s, gating.ENTANGLEMENT_SOURCE_DECLARED
            )
        except (TypeError, ValueError):
            pass
    return gating.EntanglementFloor.unknown()


def build_gate_disclosure(
    block: Mapping[str, Any] | None,
    *,
    declared_first_bounce_s: float | None = None,
) -> GateDisclosure:
    """THE single reader of a persisted gating block into a typed record.

    Defensive by contract: this runs on records written by older builds and
    on best-effort diagnostic paths that must never raise, so every field is
    validated rather than trusted, and a missing or malformed one becomes
    ``None`` — "unknown", never a fabricated value. A schema-1 block (whose
    ``first_reflection_ms`` is an ONSET, not an arrival) reads cleanly; its
    absent fields come back ``None`` and its onset never times the
    entanglement floor (:func:`_entanglement_floor`).

    ``declared_first_bounce_s`` is the operator's geometry, in seconds, and
    is the entanglement floor's fallback source — used only when the gating
    block measured no reflection to time (#3502). It is not persisted in the
    block because it is not the block's fact.
    """
    b: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    source = b.get("floor_source")
    exempt = b.get("exempt_reason")
    delta = b.get("pre_post_gate_delta")
    delta = delta if isinstance(delta, Mapping) else {}
    band = delta.get("eval_band_hz")
    ledger = b.get("internal_reflection_ledger")
    entries = [e for e in ledger if isinstance(e, Mapping)] if isinstance(
        ledger, (list, tuple)
    ) else []
    floor_source = source if isinstance(source, str) else None
    direct_peak_ms = _finite(b.get("direct_peak_ms"))
    reflection_toa_ms = _finite(b.get("first_reflection_ms"))
    entanglement = _entanglement_floor(
        floor_source,
        _relative_delay_ms(reflection_toa_ms, direct_peak_ms),
        declared_first_bounce_s,
        schema_version=b.get("schema_version"),
    )
    return GateDisclosure(
        gate_ms=_finite(b.get("window_ms")),
        source_of_bound=floor_source,
        exempt_reason=exempt if isinstance(exempt, str) else None,
        f_min_hz=_finite(b.get("f_valid_floor_hz")),
        f_trusted_hz=_finite(b.get("f_trusted_hz")),
        direct_peak_ms=direct_peak_ms,
        reflection_toa_ms=reflection_toa_ms,
        delta_rms_db=_finite(delta.get("rms_db")),
        delta_band_hz=(
            (float(band[0]), float(band[1]))
            if isinstance(band, (list, tuple))
            and len(band) == 2
            and _finite(band[0]) is not None
            and _finite(band[1]) is not None
            else None
        ),
        internal_reflection_count=sum(
            1 for e in entries if e.get("classification") == gating.CLASS_DUT_INTERNAL
        ),
        ledger_count=len(entries),
        entanglement_floor_hz=entanglement.hz,
        entanglement_floor_source=entanglement.source,
    )


def describe_gate(
    block: Mapping[str, Any] | None,
    *,
    declared_first_bounce_s: float | None = None,
) -> str:
    """:func:`build_gate_disclosure` then :func:`render_gate`, for a caller
    holding a block rather than a typed record.

    ``declared_first_bounce_s`` reaches :func:`build_gate_disclosure`
    unchanged. A caller that already built the record must call
    :func:`render_gate` instead: building it twice from one block is two
    readings of one fact, and only the second one is rendered.
    """
    return render_gate(
        build_gate_disclosure(block, declared_first_bounce_s=declared_first_bounce_s)
    )


def render_gate(d: GateDisclosure) -> str:
    """The one honest sentence about a gate. THE single writer of it.

    This is the #1966 fix at the surface: a record that prints
    ``gate_window_ms = 7.0`` and nothing else reads as "reflections
    removed" to every downstream consumer, and across the whole 2026-07-30
    corpus it meant "no reflection found; window capped at the ceiling".
    Those two states get different words here, and the ceiling case says
    explicitly that nothing was gated out.

    Consumers render this verbatim. They do not re-phrase the fields —
    re-phrasing is how the two states started printing identically.
    """
    if d.exempt_reason:
        return (
            f"gating not applicable ({d.exempt_reason}); no reflection "
            "search ran and no validity floor was established"
        )
    if d.source_of_bound is None:
        # The room's floor is the operator's geometry here, and it survives a
        # capture the gate could not use at all — the exempt branch above is
        # the one case with no room floor worth stating (a near-field capture
        # taken a few centimetres from the driver).
        return (
            "this capture could not be gated; no reflection search ran and "
            "no validity floor was established"
        ) + _entanglement_clause(d)

    floors = ""
    if d.f_min_hz is not None:
        floors = f", valid above {d.f_min_hz:.0f} Hz"
        if d.f_trusted_hz is not None:
            floors += f" and trusted above {d.f_trusted_hz:.0f} Hz"

    if d.gated_anything:
        delay = d.reflection_delay_ms
        toa = f" at {delay:.2f} ms" if delay is not None else ""
        # "to its ONSET", not "to it": the window ends where the reflection
        # begins, which is EARLIER than the arrival reported beside it. Say
        # which of the two the gate used, or the two numbers read as a
        # contradiction.
        head = (
            f"reflection measured{toa} after the direct arrival; window "
            f"gated to its onset at {d.gate_ms:.2f} ms{floors}"
            if d.gate_ms is not None
            else f"reflection measured{toa} after the direct arrival{floors}"
        )
    else:
        gate = f"{d.gate_ms:.2f} ms" if d.gate_ms is not None else "the ceiling"
        head = (
            f"no reflection found; window capped at the {gate} search "
            f"ceiling, so nothing was gated out{floors}"
        )

    return head + _entanglement_clause(d) + _ledger_clause(d) + _delta_clause(d)


def _entanglement_clause(d: GateDisclosure) -> str:
    """The room's floor, appended beside the window's two — never instead of
    them, and never collapsed into one phrase with them.

    The unknown branch names the action that resolves it, because a reader
    told only that a number is missing has no way to stop it being missing.
    """
    floor = d.entanglement_floor_hz
    if floor is None:
        return (
            "; the room's entanglement floor is unknown (declare the rig's "
            "geometry with jasper-declare-geometry to resolve it) — no bin between the "
            "trusted floor and the room's floor is proven clean"
        )
    provenance = (
        "the measured reflection"
        if d.entanglement_floor_source == gating.ENTANGLEMENT_SOURCE_MEASURED
        else "declared geometry"
    )
    return f"; room-entangled below {floor:.0f} Hz (floor from {provenance})"


def _ledger_clause(d: GateDisclosure) -> str:
    """The asymmetric-cost guard's disclosure, when it caught something.

    Counts ONLY the loudspeaker-internal classification. A ``gateable``
    candidate the detector did not select is a different statement and
    saying "loudspeaker-internal" about it would be the same class of
    overstatement this whole contract exists to remove.
    """
    n = d.internal_reflection_count
    if not n:
        return ""
    noun = "feature" if n == 1 else "features"
    return (
        f"; {n} early {noun} arriving before the search window opens, "
        "classified as loudspeaker-internal and deliberately not gated"
    )


def _delta_clause(d: GateDisclosure) -> str:
    """The delta, never rendered without what makes it readable.

    A small delta means "genuinely clean" on a measured bound and "the gate
    did nothing, and nothing was proven" on a ceiling-capped one — see this
    module's docstring. The two readings differ only by
    ``source_of_bound``, so the clause states which one applies rather than
    leaving the number to be read either way.
    """
    if d.delta_rms_db is None:
        return ""
    band = (
        f" over {d.delta_band_hz[0]:.0f}-{d.delta_band_hz[1]:.0f} Hz"
        if d.delta_band_hz is not None
        else ""
    )
    clause = f"; the gate moved the response {d.delta_rms_db:.2f} dB RMS{band}"
    if d.delta_rms_db >= SMALL_DELTA_RMS_DB:
        return clause
    return clause + (
        " — genuinely little to remove here"
        if d.gated_anything
        else " — but with no reflection found, that proves nothing about reflections"
    )
