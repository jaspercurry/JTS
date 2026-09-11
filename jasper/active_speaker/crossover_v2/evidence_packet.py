# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One round's banked evidence, gathered into one document a reader can answer.

Grades nothing and writes nothing. Its one impurity is reading JSON files
under a directory: no clock, no network, no CamillaDSP handle, no session. It
DERIVES exactly two things — :func:`_cross_seat_sigma_block`'s per-bin spread
across seats, and :func:`_reflections_block`'s tau-to-path-length multiply.

Absence has two never-merged flavours: ``source_absent`` (the artifact was not
handed to this builder) and ``field_null`` (it was, and the field is null).
Redaction is an allowlist that publishes the names it withheld. Operator prose
enters in exactly one block, quarantined and named in ``privacy`` — see
:func:`_operator_notes_block`.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)
# The repo's ONE speed of sound, consumed rather than restated — the same
# constant ``program_analysis.MeasurementGeometry`` and ``branch_chain`` import.
# It is a plain float in a stdlib-only module, so this costs no cycle.
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.program_analysis import ABSOLUTE_NO_CROSSOVER_TOPOLOGY
from jasper.json_fields import finite_float

from ..commissioning_evidence_store import EVIDENCE_ROOT
from ..repeat_floor import REPEAT_FLOOR_KIND, load_repeat_floor, stopping_thresholds
from .contracts import POSITION_EVIDENCE_KIND
from .journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from .record_index import Measurement, bundle_measurements
from .round_inputs import (
    STATE_SESSION_UNKNOWN,
    state_matches_capture, contract_sources,
    CrossoverEvidencePacketError, NO_ROUND_ARTIFACTS_REASON,
    recent_round_sessions, round_artifact_dir,
)
# The MODULE, not the function: ``position_cycle`` owns the accept rule, and
# resolving it through the module on every call is what keeps that ownership
# real rather than a copy taken once at import.
from . import position_cycle
from . import handoff_doors as doors
from .prescription_contract import (
    CONTRACT_COMMAND, contract_digests, prescription_contracts, snr_shape,
)
from .driver_prescription import (
    driver_passbands_from_safety_profile,
)
from .feature_classification import (
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    UNCERTAINTY_RANDOM,
    UNCERTAINTY_SYSTEMATIC,
    UNCERTAINTY_UNSEPARATED,
    FeatureVerdict,
    read_feature_verdicts,
)
from ..installation import installation_evidence
from .operator_notes import OPERATOR_NOTES_KIND, build_operator_notes
from .round_evidence import ITERATION_PLATEAU_DB, MEASURED_BENEFIT_MARGIN_DB

__all__ = [
    "CLASSIFICATION_ARTIFACT",
    "HARMONICS_ARTIFACT",
    "NO_CANDIDATE_TAKES",
    "NO_ROUND_ARTIFACTS_REASON",
    "OPERATOR_NOTES_BLOCK",
    "validate_packet",
    "PACKET_KIND",
    "PACKET_SCHEMA_VERSION",
    "RING_SIDECAR_GLOB",
    "CrossoverEvidencePacketError",
    "build_crossover_evidence_packet",
    "packet_driver_passbands_hz",
    "packet_feature_classifications",
    "packet_incumbent_linearization",
    "packet_positional_evidence",
    "packet_region_band_hz",
    "round_artifact_dir",
    "round_program_dir",
]

#: Bumped when a reader that understood the previous version would misread
#: this one — never merely because the document grew. The
#: EVIDENCE document's version only: a prescription answering this packet
#: carries its own :data:`~.blend_prescription.PRESCRIPTION_SCHEMA_VERSION`.
PACKET_SCHEMA_VERSION = 2

PACKET_KIND = "jts_crossover_v2_evidence_packet"

#: The one block that carries operator prose. Named in ``privacy`` so the
#: document points at its own quarantine, and asserted to RESOLVE by
#: ``test_the_packet_points_at_its_own_quarantine``.
OPERATOR_NOTES_BLOCK = "operator_notes"

GENERATED_BY = (
    "jasper.active_speaker.crossover_v2.evidence_packet."
    "build_crossover_evidence_packet"
)

#: Position fields copied verbatim. ``wav_path`` is deliberately absent — it is
#: an absolute path on the speaker's filesystem, and ``wav_sha256`` identifies
#: the same bytes without naming where they live.
_POSITION_FIELDS = (
    "position_id",
    "index",
    "attempt",
    "role",
    # WHERE the capture was taken, copied through from the same
    # ``_RECORD_FIELDS`` join every other per-position scalar rides;
    # :func:`_angle_deg_block` is conditional on what the rows actually carry.
    "position_deg",
    "position_axis",
    # The elevation half of the same WHERE, orthogonal to the bearing: a seat
    # raised above mark height states both.
    "vertical_deg",
    "mark_distance_m",
    "take_id",
    "wav_sha256",
    "validity_floor_hz",
    "gate_disclosure",
    "gate_floor_source",
    # The two NUMBERS ``gate_disclosure`` narrates, beside it rather than
    # instead of it: the sentence makes a small ``gate_moved_rms_db``
    # readable, the number makes the sentence usable without parsing English.
    "gate_moved_rms_db",
    "gate_reflection_delay_ms",
    # The ROOM's floor at this seat and where it came from — always a pair,
    # because the number is unreadable without its provenance.
    "gate_entanglement_floor_hz",
    "gate_entanglement_floor_source",
    "gate_window_ms",
    "gating_applied",
    "glitch_detected",
    "summed_ripple_db",
    "reverse_null_depth_db",
    "echo",
)

#: Bundle identity fields copied verbatim from ``info.json``'s ``fingerprints``
#: block. The mic sub-block is copied whole: it carries a calibration id and a
#: content hash, never a serial (``household_mic`` keeps only a one-way
#: ``serial_hash`` and a last-4 display, and neither is in this tree).
_IDENTITY_FIELDS = (
    "topology_id",
    "topology_fingerprint",
    "output_assignments",
    "graph_fingerprint",
    "mic",
    "build_sha",
)

#: Where a round's banked feature classification lives, if one was banked.
#: One name shared by the instrument that writes it (:mod:`.feature_classifier`
#: via ``jasper-round-views classify-features``), this packet, and the gate
#: that acts on
#: it. No stage of a round writes it automatically — it is an offline run — so
#: its absence is an ordinary reported ``source_absent``.
CLASSIFICATION_ARTIFACT = "feature_classification.json"

#: The round's banked harmonic-distortion reading, beside the classification.
#: Same posture as :data:`CLASSIFICATION_ARTIFACT`; written offline by
#: ``jasper-round-views distortion`` over :mod:`.harmonic_evidence`. Defined HERE
#: rather than in that module because it imports this one (for
#: :data:`RING_SIDECAR_GLOB`): the packet owns the names of what it reads.
HARMONICS_ARTIFACT = "harmonic_distortion.json"

#: The three phases a finding set is banked under, each at its own
#: ``findings_{phase}.json``
#: (:func:`~jasper.attribution.storage.findings_relative_path`): the two
#: cloud-group closes and the level-frame gate's own MEASURE-phase set.
_FINDING_PHASES = (PHASE_MEASURE, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY)

#: The phases whose set comes from carve-out promotion, which reads only the
#: cloud group's ``echo_band_hz``: a feature outside that band cannot become a
#: finding in one, whatever the round measured. The MEASURE set is the
#: level-frame gate's own and carries the band of the record it came from.
_ECHO_BAND_PHASES = (PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY)

#: Where a round banks one JSON record per accepted take, INSIDE the round
#: directory :func:`round_artifact_dir` returns:
#: :meth:`~.record_store.BankedRecordStore.bank` publishes
#: ``crossover_v2/{capture}/positions/{take_id}.json`` under
#: ``{EVIDENCE_ROOT}/artifacts/``. :mod:`.position_cycle` reaches the same
#: files from the BANKED ROUND root, which is why the accept rule is imported
#: from there rather than restated here.
_POSITIONS_SUBDIR = "positions"

#: How a capture ring's sidecars are found under the ring root. No round
#: writes this layout on the Pi any more; the rings that reach the two readers
#: are corpora pulled off a Pi before the retention seam died and rings
#: :func:`~.ring_projection.project_ring` re-projects laptop-side.
#:
#: ``**/`` because a pull splits the speaker's flat ring into ``dumps/wav/`` +
#: ``dumps/sidecar/``, with a per-phase nesting of that shape too, so a caller
#: passes the ring ROOT and the pattern finds the sidecars inside it. Both
#: readers (:func:`~.feature_classifier.load_round_captures` and
#: :func:`~.harmonic_evidence.read_round_harmonics`) consume this constant, so
#: ``--dumps`` cannot mean two different directories.
RING_SIDECAR_GLOB = "**/sidecar/*.json"

#: What :func:`_capture_snr_block` reads off one banked take: the two
#: identities the packet's other take rows already carry, the digest of the
#: stimulus that was PLAYED (a different quantity from ``wav_sha256``, which
#: is the captured audio's), the phase that says which capture it was, and the
#: analysis block the SNR columns live in.
_TAKE_DIAGNOSTIC_FIELDS = (
    "take_id", "wav_sha256", "stimulus_wav_sha256", "phase", "diagnostic",
)

#: The substring that identifies a signal-to-noise field in a banked take's
#: flat ``diagnostic`` block. A substring rather than a name list because the
#: producer
#: (:func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`)
#: composes most names onto a ROLE the packet cannot know, so no allowlist here
#: could enumerate them.
_DIAGNOSTIC_SNR_MARKER = "snr"






#: Verify-claim and state fields the packet carries. ``household_findings`` is
#: NOT among them and never will be: it is household-authored prose, the one
#: privacy-sensitive field in the tree. The operator prose in
#: :data:`OPERATOR_NOTES_BLOCK` is the opposite decision, and the difference is
#: the WRITER: a commissioning declaration about the hardware being graded,
#: not copy a household typed into a correction carve-out.
_STATE_WITHHELD = ("household_findings",)



def _read_json(path: Path) -> tuple[Any, str]:
    """One artifact, plus why it is missing when it is.

    Never raises on a bad file: an unreadable artifact is a fact about the
    round that the packet reports, not a reason to have no packet at all.
    """
    if not path.exists():
        return None, "source_absent"
    try:
        return json.loads(path.read_text()), ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"unreadable: {type(exc).__name__}"
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg}"


def applied_profile_source(path: Path | None) -> tuple[dict[str, Any] | None, str]:
    """The applied-profile SSOT, and why there is none when there is none.

    One owner for "what is this speaker playing":
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`.
    It collapses every failure into ``None``, so the REASON is read separately
    on that path. A file that parsed but the loader rejected has three causes,
    and rather than re-derive that verdict here the reason ECHOES the
    document's own three self-describing fields.
    """
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )

    if path is None:
        return None, "no applied baseline profile was supplied"
    profile = load_applied_baseline_profile_state(path)
    if profile is not None:
        return profile, ""
    raw, reason = _read_json(path)
    if reason:
        return None, reason
    document = _mapping(raw)
    return None, (
        "the file is not an applied baseline profile this install can read "
        f"(kind={document.get('kind')!r}, "
        f"artifact_schema_version={document.get('artifact_schema_version')!r}, "
        f"status={document.get('status')!r})"
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _declared_geometry_block(path: Path | None) -> dict[str, Any]:
    """The household's own tape measure, in metres, or WHICH absence this is.

    Read from the path the CALLER resolved — a banked round's frozen sibling,
    or the live speaker's own SSOT file. ``None`` is a round that banked no
    declaration, which is a different fact from one this install could not
    read.
    """
    from jasper.audio_measurement.measurement_geometry import (
        load_declared_geometry,
    )

    if path is None:
        return _absence("source_absent", False, "declared_geometry")
    try:
        geometry = load_declared_geometry(path)
    except (OSError, ValueError) as exc:
        return _absence(f"unreadable: {type(exc).__name__}", False, "declared_geometry")
    if geometry is None:
        return _absence("source_absent", False, "declared_geometry")
    return geometry.to_dict()


def _ordinal(value: Any) -> int:
    """A sort key from an identity field, or ``0`` when it is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _absence(source_reason: str, present: bool, field: str) -> dict[str, Any]:
    """Which of the two absences this is, said explicitly.

    ``source_absent`` when the artifact never arrived, ``field_null`` when it
    did and the field inside it is null. Merging them is the reading defect
    this packet exists partly to fix.
    """
    if source_reason:
        return {"status": "not_evaluated", "reason": source_reason, "field": field}
    if not present:
        return {"status": "not_evaluated", "reason": "field_null", "field": field}
    return {}


def _copy_allowed(
    raw: Any, allowed: tuple[str, ...]
) -> tuple[dict[str, Any], list[str]]:
    """Named fields through, and the names of everything held back.

    Reporting the withheld NAMES is the part that matters: a packet that
    silently narrowed its source would be a different document wearing the same
    schema version.
    """
    if not isinstance(raw, dict):
        return {}, []
    kept = {key: raw[key] for key in allowed if key in raw}
    withheld = sorted(key for key in raw if key not in allowed)
    return kept, withheld


def _exact_json_value(value: Any, column: str, non_finite: set[str]) -> Any:
    """One copied value as exact JSON, naming any column that was not.

    Two inputs legitimately carry ``NaN``: a classification row (the instrument
    writes one for ``z_local``, ``frac_of_nmp`` and ``excess_loss_vs_null``
    when the underlying scale is zero) and a dump-ring sidecar. Both are banked
    with a plain ``json.dumps``, which writes ``NaN`` verbatim, and
    :func:`~jasper.audio_measurement.evidence_identity.json_fingerprint`
    refuses a non-finite number — so copying one through would cost the round
    its whole packet.

    A non-finite number therefore becomes ``null`` and its COLUMN is named in
    the block's ``non_finite_fields``: "not computable" and "not carried" are
    different facts. Recursive because three classification columns are
    per-gate tables and one is a list.

    No ``bool`` guard is needed: ``bool`` subclasses ``int``, never ``float``,
    so a boolean column falls through to the passthrough already.

    Scoped to those two blocks. Every other input is written with
    ``allow_nan=False`` and structurally cannot carry one, except the
    ``incumbent`` block's filters, which come from the applied-profile SSOT's
    plain ``json.dumps``: a non-finite gain there would reach
    :func:`_fingerprint` and cost the packet. Disclosed rather than guarded —
    routing that block through this function is the fix if one is observed.
    """
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        non_finite.add(column)
        return None
    if isinstance(value, dict):
        return {
            key: _exact_json_value(item, column, non_finite)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_exact_json_value(item, column, non_finite) for item in value]
    return value



def round_program_dir(
    session_dir: Path, round_dir: Path, phases: Iterable[str]
) -> Path:
    phases = tuple(phases)
    for directory in (round_dir, session_dir / "crossover_v2" / round_dir.name):
        if any(
            (directory / f"{phase}_program.wav").is_file()
            or any(directory.glob(f"{phase}_*_program.wav"))
            for phase in phases
        ):
            return directory
    return round_dir


#: Decimal places the cross-seat spread is published to. Four, matching the
#: member curves it is taken over
#: (:func:`~jasper.attribution.position_evidence._sample_onto`): more digits
#: than its own inputs would be false precision, and this document is
#: content-fingerprinted.
_SIGMA_DECIMALS = 4

#: The cross-seat spread, declared as the enrichment rule requires. Entry
#: shape is :data:`~.feature_classification.LAB_ROW_UNCERTAINTY`'s (``kind`` +
#: ``of``); what differs is the LIST it publishes under, because
#: :data:`~.feature_classification.UNCERTAINTY_UNSEPARATED` is deliberately not
#: a member of the closed kind set.
_CROSS_SEAT_SIGMA_UNCERTAINTY: dict[str, dict[str, str]] = {
    "per_bin_sigma_db": {
        "kind": UNCERTAINTY_UNSEPARATED,
        "of": (
            "how far the seats disagree at this bin — the sample standard "
            "deviation (ddof=1) across the member curves above, uncentred. It "
            "contains TWO spreads and separates neither: the sound field's real "
            "variation from seat to seat, which no amount of repeating at a "
            "fixed seat would reduce, and the measurement noise each member "
            "curve carries, which averaging repeats into each member would. "
            "Which of the two dominates cannot be read off this round — "
            "separating them needs a repeat spread "
            "measured at a FIXED pose, which is the banked repeat floor "
            "(accuracy_budget.in_capture_repeat_floor), available on a rig "
            "that banked one. So it is published as a spread whose kind is not "
            "yet separable, never as a random or a systematic one: calling it "
            "either would be exactly the pooling these labels exist to prevent"
        ),
    },
}

#: Fields of the cross-seat block that are not spreads. ``n_seats`` is the
#: load-bearing one: this block publishes n, so it has to say what the obvious
#: quotient would and would not mean.
_CROSS_SEAT_SIGMA_NOT_AN_UNCERTAINTY: dict[str, str] = {
    "n_seats": (
        "how many member curves the spread above was taken over. A count, not a "
        "spread — published because a standard deviation cannot be judged "
        "without its n, and a reader not given one counts the rows anyway. It is "
        "not a divisor to reach for while the spread's two halves are "
        "unseparated: per_bin_sigma_db/sqrt(n_seats) would be the standard error "
        "of the cross-seat MEAN, and only the random half falls that way, so "
        "until the halves are separated that quotient is the standard error of "
        "nothing"
    ),
    "n_seats_excluded": (
        "member curves this block could not use — a row with no magnitude_db, "
        "one whose length does not match the grid, one carrying a sample that "
        "is not a real number, or EVERY row when the positions block carried no "
        "curve grid at all, because with no bins to check a length against no "
        "row can be read as a curve on this grid. That fourth cause is why the "
        "count can equal the row total while nothing was individually rejected; "
        "the block's reason says which case produced it. Counted rather than "
        "dropped quietly, on the rule the capture_snr block keeps for the "
        "takes it does not publish. A count, not a spread"
    ),
}


def _member_curve(values: Any, n_bins: int) -> list[float] | None:
    """One member curve as ``n_bins`` real numbers, or ``None``.

    All-or-nothing per row: a curve admitted for only the bins it could supply
    would make ``n_seats`` not the count the spread was taken over in every
    bin. The row is counted, never dropped silently.
    """
    if not isinstance(values, list) or len(values) != n_bins:
        return None
    curve: list[float] = []
    for value in values:
        number = finite_float(value)
        if number is None:
            return None
        curve.append(number)
    return curve


def _cross_seat_sigma_block(
    freqs_hz: Any, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """How far the seats disagree, bin by bin — the packet's one computed number.

    The sample standard deviation (``ddof=1``) across the member curves, per
    bin, index-aligned with ``curve_grid.freqs_hz``. Taken over the rows THIS
    packet publishes, so a reader can reproduce the figure from the packet
    alone. Nothing upstream publishes it: the combiner forms the same array in
    :func:`~jasper.audio_measurement.spatial_combine._band_spread` and reduces
    it to two figures per octave band without letting the array out.

    It would be a different statistic if it did. The combiner's runs over
    ``per_position_db``, raw and unsmoothed on a LINEAR grid; the member curves
    here are ``per_position_diag_db``, smoothed at the diagnostic fraction and
    resampled onto a LOG 1/12-octave grid whose floor is the round's validity
    floor when it has a usable one and 20 Hz otherwise
    (``curve_grid.floor_source`` says which). Hence the name ``per_bin_sigma``
    rather than ``sigma_db`` or ``max_sigma_db``, which already mean the
    combiner's own reductions.

    It lives inside the positions block, beside the grid it is on: a reader
    must not be able to pair a spread with a grid it was not taken over.

    Below two usable seats it refuses and does not publish 0.0 — a sample
    standard deviation is undefined at n=1, and a zero would say the seats
    agreed.

    ``statistics.stdev`` rather than numpy: it RAISES at n < 2 instead of
    returning a silent ``NaN``, and computes in exact arithmetic, so a spread
    too large for a float is an ``OverflowError`` rather than an ``inf`` that
    would reach the fingerprint. Do not weaken the guard or the ``except``.
    Exact arithmetic costs 1.8 ms for the shipped shape (4 seats over an
    89-bin grid) and 54 ms for a pessimistic 12 x 2048; the packet is built by
    an offline CLI, never on an audio path.
    """
    n_bins = len(freqs_hz) if isinstance(freqs_hz, list) else 0
    if not n_bins:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                "the positions block carries no curve grid, so there are no "
                "bins to take a spread over"
            ),
            "n_seats": 0,
            "n_seats_excluded": len(rows),
        }
    curves: list[list[float]] = []
    excluded = 0
    for row in rows:
        curve = _member_curve(row.get("magnitude_db"), n_bins)
        if curve is None:
            excluded += 1
        else:
            curves.append(curve)
    if len(curves) < 2:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                f"a spread across seats needs two usable member curves and this "
                f"round has {len(curves)} ({excluded} row(s) could not be read "
                f"as a curve on this grid). A sample standard deviation is "
                f"UNDEFINED at one seat, so nothing is published — a 0.0 here "
                f"would say the seats agreed"
            ),
            "n_seats": len(curves),
            "n_seats_excluded": excluded,
        }
    try:
        per_bin_sigma_db = [
            round(
                statistics.stdev(curve[index] for curve in curves), _SIGMA_DECIMALS
            )
            for index in range(n_bins)
        ]
    except OverflowError:
        # Reachable: ``statistics.stdev`` raises rather than returning ``inf``
        # when the exact result will not fit a float, which a hand-edited
        # member curve near the float ceiling does.
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                "a member curve carries samples so large that their spread does "
                "not fit a float; this artifact cannot be read for a cross-seat "
                "spread at all"
            ),
            "n_seats": len(curves),
            "n_seats_excluded": excluded,
        }
    return {
        "available": True,
        "n_seats": len(curves),
        "n_seats_excluded": excluded,
        "per_bin_sigma_db": per_bin_sigma_db,
        "source": "positions[].magnitude_db, across seats, one value per grid bin",
        "uncertainty": {
            # Empty, and that is the answer rather than an omission — see note.
            "fields": {},
            "not_uncertainties": dict(
                sorted(_CROSS_SEAT_SIGMA_NOT_AN_UNCERTAINTY.items())
            ),
            # The third list, for the case neither of the first two describes: a
            # real spread about a reading whose kind this evidence cannot say.
            "unseparated": {
                field: dict(entry)
                for field, entry in sorted(_CROSS_SEAT_SIGMA_UNCERTAINTY.items())
            },
            "note": (
                "fields is empty because nothing here is a random OR a "
                "systematic uncertainty: the one spread published is a pooling "
                "of both, so it is declared under unseparated rather than filed "
                "as a kind it does not have. The rule that produces that "
                "answer: the two kinds are never pooled into one number, and "
                "where a measurement can only yield a pooled one it says so and "
                "names what would separate it — here, a repeat spread at a "
                "fixed pose, which is the banked repeat floor "
                "(accuracy_budget.in_capture_repeat_floor)"
            ),
        },
        "note": (
            "one value per curve_grid.freqs_hz bin, in that order, computed "
            "from the position rows THIS packet publishes — so it is "
            "reproducible from the packet alone. UNCENTRED: a seat that simply "
            "plays louder raises it, because a level difference between seats "
            "is part of what 'the seats disagree' means here. It is not the "
            "combiner's sigma_db/max_sigma_db under another name: those are "
            "taken over raw unsmoothed curves on a linear grid and reduced to "
            "two figures per octave band, they describe the cloud_measure "
            "group, and they reach only candidate.json's exclusion_evidence — "
            "not this document, and not the cloud evidence it is built from"
        ),
    }


def _positions_block(cloud: dict[str, Any]) -> dict[str, Any]:
    """Per-position curves and capture integrity, copied rather than derived.

    The grid, the curves and the flat reference all come from ONE artifact, so
    a reader (and :func:`~.blend_prescription.positional_support`) cannot
    compare a curve from one evaluation against a reference from another. The
    one DERIVED field is ``cross_seat_sigma``, which sits here so a spread and
    the grid it was taken over are not separable.
    """
    positions = _mapping(cloud.get("positions"))
    grid = _mapping(positions.get("curve_grid"))
    rows: list[dict[str, Any]] = []
    withheld: set[str] = set()
    for entry in positions.get("positions") or []:
        if not isinstance(entry, dict):
            continue
        kept, dropped = _copy_allowed(entry, _POSITION_FIELDS + ("magnitude_db",))
        withheld.update(dropped)
        rows.append(kept)
    freqs_hz = grid.get("freqs_hz") or []
    return {
        "available": bool(rows),
        "schema": positions.get("schema"),
        "n_positions": len(rows),
        "curve_grid": {
            "freqs_hz": freqs_hz,
            "fractional_octave": grid.get("fractional_octave"),
            "smoothing_fraction": grid.get("smoothing_fraction"),
            "floor_hz": grid.get("floor_hz"),
            "floor_source": grid.get("floor_source"),
        },
        "positions": rows,
        # Derived from the two above and published beside them, so a reader
        # cannot pair the spread with a grid it was not taken over.
        "cross_seat_sigma": _cross_seat_sigma_block(freqs_hz, rows),
        "redacted_fields": sorted(withheld),
        # The bearings this round's own seats were prompted at — or, for a
        # round whose records predate the writer, why there are none.
        "angle_deg": _angle_deg_block(rows),
        "role_vocabulary": sorted({
            str(row.get("role")) for row in rows if row.get("role")
        }),
    }


def _angle_deg_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The cloud seats' own bearings, or the reason this round banks none.

    :func:`~.spatial.cloud_position_record` stamps ``position_deg`` /
    ``position_axis`` / ``mark_distance_m`` on every retained cloud position,
    so the absence branch states only the NARROW fact: THIS round's rows carry
    no bearing. It names ``position_axis`` and ``role`` as the fields that
    separate the ways that happens — a vertical seat and a geometry-retake seat
    both legitimately bank no degree.

    ``bool`` subclasses ``int``, so a ``true`` in the field would otherwise
    publish 1 as a bearing.
    """
    angles = sorted({
        row["position_deg"] for row in rows
        if isinstance(row.get("position_deg"), int)
        and not isinstance(row.get("position_deg"), bool)
    })
    if angles:
        return {
            "available": True,
            "angles_deg": angles,
            "note": (
                "position_deg is signed whole degrees, negative LEFT of the "
                "design axis, read off each seat's own record rather than "
                "parsed out of its prompt. A seat may carry none — a vertical "
                "pose commands no bearing, and neither does a geometry-locked "
                "retake, whose record declares no side — so this set can "
                "be shorter than n_positions, and positions[].position_axis "
                "with positions[].role says which seats are missing from it. "
                "These are cloud seats, not the lateral walk's poses in the "
                "lateral_poses block; the two are different captures and "
                "share no row."
            ),
        }
    return {
        "available": False,
        "status": "not_evaluated",
        "reason": (
            "no position row in this round carries position_deg, so no seat in "
            "it states a bearing. Different rounds look like this and "
            "positions[].position_axis with positions[].role separates them: "
            "one banked before the capture-time writer gained the field, and "
            "one whose seats commanded no bearing at all — a vertical pose, or "
            "a geometry-locked retake, whose record declares no side"
        ),
    }


def _read_take_diagnostic(path: Path) -> dict[str, Any] | None:
    """One banked take narrowed to its identity and its analysis, or ``None``.

    Takes every phase, because an SNR is an SNR whichever capture produced it.
    """
    raw, _ = _read_json(path)
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    return {field: raw.get(field) for field in _TAKE_DIAGNOSTIC_FIELDS}


def _banked_takes(
    session_dir: Path,
    rows: Sequence[Measurement],
    phase: str | None,
    read: Callable[[Path], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Every banked take of one ``phase``, narrowed by its own accept rule.

    ``rows`` is the bundle's measurement index, scanned ONCE per packet
    (:func:`~.record_index.bundle_measurements` rescans on every call).
    ``phase`` of ``None`` takes every take the round banked. ``read`` still
    OPENS each selected file and may reject it: the index narrows the
    candidates, the record decides.
    """
    artifacts = session_dir / EVIDENCE_ROOT / "artifacts"
    takes = [
        read(artifacts / row.path)
        for row in rows
        if phase is None or row.phase == phase
    ]
    return [take for take in takes if take is not None]


def _distinct_degrees(takes: list[Any], field: str) -> list[int]:
    """The sorted whole degrees ``field`` carries across ``takes``."""

    values = set()
    for take in takes:
        value = take.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            values.add(value)
    return sorted(values)


def _lateral_poses_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """The signed bearings a lateral walk banked, one row per accepted take.

    Read through :func:`~.position_cycle.read_lateral_take`, the same accept
    rule :func:`~.position_cycle.position_cycle_document` uses for these files.

    Beside the ``positions`` block, never merged into it: a cloud position is a
    summed sweep at a floor-plan seat judged by gating and ripple, a lateral
    pose is a per-driver measurement at a bearing on the design axis.

    ``position_deg`` is the SIGNED whole-degree bearing, negative LEFT of the
    design axis, stamped by :func:`~.spatial.lateral_pose_record` at take time.
    A commanded pose recorded verbatim, not a measurement with a spread, so
    this block publishes no uncertainty.

    Both survivors and superseded takes are listed, because the speaker keeps
    both on disk deliberately.
    """
    takes = _banked_takes(
        session_dir, rows, PHASE_LATERAL, position_cycle.read_lateral_take,
    )
    if not takes:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                f"this round banked no {PHASE_LATERAL} take records under "
                f"{_POSITIONS_SUBDIR}/ — its walk was refused at take time, its "
                "poses were never accepted, or the round ran no lateral walk "
                "at all"
            ),
            "n_takes": 0,
        }
    # Coerced rather than cast: a hand-edited sidecar with a non-numeric index
    # sorts first instead of raising.
    takes.sort(key=lambda take: (_ordinal(take["index"]), _ordinal(take["attempt"])))
    return {
        "available": True,
        "n_takes": len(takes),
        "takes": takes,
        # ``bool`` subclasses ``int``, so a ``true`` in either field would
        # otherwise publish 1 as a degree.
        "angles_deg": _distinct_degrees(takes, "position_deg"),
        "elevations_deg": _distinct_degrees(takes, "vertical_deg"),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json",
        "note": (
            "position_deg is signed whole degrees, negative LEFT of the design "
            "axis. These are LATERAL walk poses, not the cloud seats in the "
            "positions block above; the two are different captures and share "
            "no row. Membership is every ACCEPTED take, a superseded attempt "
            "included — which is a different set from the conductor's live "
            "lateral_poses, where a retake replaces the attempt it supersedes "
            "and only the latest per index survives"
        ),
    }


#: Why there is no block at all: no banked take names a candidate. The
#: ``jasper-measure`` door refuses to bank a variant take without one, so this
#: is a round that cycled no candidates rather than one that lost their labels.
NO_CANDIDATE_TAKES = "no_candidate_takes"


def _candidates_block(rows: Sequence[Measurement]) -> dict[str, Any]:
    """Which candidates this round played, and at which poses.

    Selected on the take index's ``candidate_id`` column across EVERY phase:
    the engine's own capture record carries a candidate id and no phase at all,
    so a phase-narrowed selection would miss the takes a candidate cycle banks.
    An INVENTORY, not a verdict.
    """
    labelled = [row for row in rows if row.candidate_id]
    if not labelled:
        return {
            "available": False,
            **_absence(NO_CANDIDATE_TAKES, False, "banked takes' candidate_id"),
        }
    by_candidate: dict[str, list[Measurement]] = {}
    for row in labelled:
        by_candidate.setdefault(row.candidate_id, []).append(row)
    candidates = []
    for candidate_id in sorted(by_candidate):
        takes = by_candidate[candidate_id]
        poses = sorted(
            {(row.position_deg, row.vertical_deg) for row in takes},
            # A take with no commanded bearing sorts last rather than raising
            # against the ints beside it.
            key=lambda pose: (pose[0] is None, pose[0] or 0, pose[1]),
        )
        candidates.append({
            "candidate_id": candidate_id,
            "n_takes": len(takes),
            "poses": [
                {"position_deg": position_deg, "vertical_deg": vertical_deg}
                for position_deg, vertical_deg in poses
            ],
        })
    return {
        "available": True,
        "candidates": candidates,
        "source": (
            f"{_POSITIONS_SUBDIR}/<take_id>.json candidate_id, selected through "
            "record_index.bundle_measurements"
        ),
        "note": (
            "two candidates measured at different poses are not comparable on "
            "these takes alone; the candidate cycle holds one pose and swaps "
            "the graph under it"
        ),
    }


def _entry_baseline_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """The round's measured "before", read from the take that banked it.

    The receipt names this capture but carries no curve, so this block is the
    durable copy — the flow state file's arrays are rewritten by the next
    persist. With it, ``verification.evaluate_benefit`` can be re-run over a
    banked round by an analysis that did not exist when it was captured.

    A round with no readable take is an ordinary reported absence: retention is
    fail-soft and never costs the household a retake.
    """
    takes = _banked_takes(
        session_dir, rows, PHASE_ENTRY_BASELINE,
        position_cycle.read_entry_baseline_take,
    )
    if not takes:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                f"this round banked no {PHASE_ENTRY_BASELINE} take record under "
                f"{_POSITIONS_SUBDIR}/ — it ran no entry baseline, its capture "
                "was refused, or evidence retention failed at take time"
            ),
        }
    # The last accepted take is the "before": a retake supersedes the attempt
    # it followed. Sorting by take_id orders by index then attempt, because the
    # id is built from both in that order.
    take = max(takes, key=lambda t: str(t.get("artifact_ref") or ""))
    return {
        "available": True,
        **take,
        "n_bins": len(take["freqs_hz"]),
        "n_excluded": sum(1 for flag in take["excluded"] if flag),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json",
        "note": (
            "the summed capture taken at the design-axis mark immediately "
            "before this round's apply. It is the durable copy: the flow state "
            "file holds the same arrays only until the next persist rewrites "
            "them. Comparable to a post-apply capture only when program_id, "
            "reference_mark and graph_fingerprint match on both sides"
        ),
    }


def _capture_snr_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """Per-capture signal-to-noise, off the round's own banked takes.

    Every accepted take carries the analysis's flat ``diagnostic`` block
    (:func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`'s
    output, written on by ``bind_position_retention``); this publishes the SNR
    columns out of it, one row per take that carried one.

    Read from the BUNDLE, so there is nothing to attribute: a take under this
    bundle's own artifacts root is this bundle's by construction. Each capture
    is named by ``take_id`` and ``wav_sha256``, the identities the
    ``lateral_poses`` and ``positions`` rows carry, so a reader can join them.
    """
    captures: list[dict[str, Any]] = []
    non_finite: set[str] = set()
    undeclared: set[str] = set()
    declared_as: dict[str, str] = {}
    seen = 0
    for take in _banked_takes(session_dir, rows, None, _read_take_diagnostic):
        seen += 1
        diagnostic = _mapping(take.get("diagnostic"))
        if not diagnostic:
            continue
        snr = {}
        for column, value in sorted(diagnostic.items()):
            if _DIAGNOSTIC_SNR_MARKER not in column:
                continue
            shape = snr_shape(column)
            if shape is None:
                undeclared.add(column)
            else:
                declared_as[column] = shape
            snr[column] = _exact_json_value(value, column, non_finite)
        # A take whose analysis reported no SNR still gets its row, with an
        # empty ``snr``: what this block does not publish, it counts.
        captures.append({
            "take_id": take.get("take_id"),
            "wav_sha256": take.get("wav_sha256"),
            "stimulus_wav_sha256": take.get("stimulus_wav_sha256"),
            "phase": take.get("phase"),
            "snr": snr,
        })
    absent: dict[str, Any] = {}
    if not captures:
        absent = {
            "status": "not_evaluated",
            "reason": (
                f"this round banked {seen} take(s) and none of them carries a "
                "diagnostic block — the round was banked before a take carried "
                "its own analysis, or every analysis it ran produced none"
            ),
        }
    return {
        "available": bool(captures),
        **absent,
        "n_captures": len(captures),
        "n_takes_seen": seen,
        "captures": captures,
        "non_finite_fields": sorted(non_finite),
        "undeclared_fields": sorted(undeclared),
        "declared_as": dict(sorted(declared_as.items())),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json, the diagnostic block",
        "uncertainty": CONTRACT_COMMAND,
        "note": (
            "one row per banked take that carried an analysis, named by the "
            "same take_id and wav_sha256 the lateral_poses and positions rows "
            "carry so a reader can join them. n_takes_seen is every take this "
            "round banked; the difference is takes whose record carries no "
            "diagnostic block at all"
        ),
    }










def _harmonics_block(raw: Any, reason: str) -> dict[str, Any]:
    """The round's banked H2/H3 reading, copied through with its declarations.

    Verbatim: the instrument that produced it (``jasper-round-views distortion``, over
    :mod:`.harmonic_evidence`) owns what the numbers mean. What this adds is
    the uncertainty declarations the artifact does not carry.

    The packet does not compute it, unlike the cross-seat spread: reading H2/H3
    means re-opening every banked capture WAV and re-deconvolving it at a
    pre-guard wide enough for the harmonic images to exist, and this module
    publishes ``privacy.raw_audio_excluded``. Absence is ordinary and reported.
    """
    if not isinstance(raw, dict):
        return {
            "available": False,
            "status": "not_evaluated",
            # NEVER the bare read reason: a file that PARSED into a non-object
            # carries the empty string, and the honest list drops any entry
            # whose reason is falsy.
            "reason": reason or (
                f"the {HARMONICS_ARTIFACT} banked for this round parsed as "
                f"{type(raw).__name__}, not as a JSON object, so there is no "
                "reading in it to publish"
            ),
            "n_roles": 0,
        }
    banked_roles = raw.get("roles")
    roles = (
        [role for role in banked_roles if isinstance(role, dict)]
        if isinstance(banked_roles, list)
        else []
    )
    if not roles:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                "a harmonic-distortion artifact is banked for this round but "
                "carries no role block, so there is no reading in it to publish"
            ),
            "n_roles": 0,
        }
    orders = [
        order for order in (raw.get("orders") or [])
        if isinstance(order, int) and not isinstance(order, bool)
    ]
    if not orders:
        # The declarations are generated FROM this list, so an artifact that
        # names no order would publish h2_/h3_ columns with nothing declaring
        # them. Refused rather than published under-declared. ``bool`` is
        # excluded above because a ``true`` would declare an "h1" nothing
        # publishes.
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                "a harmonic-distortion artifact is banked for this round but "
                "names no harmonic order, so nothing says what its rows are "
                "readings OF and no column in them could be declared"
            ),
            "n_roles": 0,
        }
    captures = _mapping(raw.get("captures"))
    return {
        "available": True,
        "artifact_schema_version": raw.get("artifact_schema_version"),
        "orders": orders,
        "n_roles": len(roles),
        "roles": roles,
        # What the instrument could NOT read, beside what it could. A round
        # where three of four captures failed the fidelity gate is a different
        # round from one where all four passed, and a reader given only the
        # survivors could not tell them apart.
        "captures": captures,
        "program": _mapping(raw.get("program")),
        # Whether a microphone calibration was applied, under which sign
        # convention, and from which banked calibration id. Load-bearing rather
        # than housekeeping: an uncalibrated read carries the microphone's own
        # response inside every ratio, and a file read under the wrong sign
        # moves every magnitude without moving one timing diagnostic.
        "calibration": _mapping(raw.get("calibration")),
        "source": HARMONICS_ARTIFACT,
        "uncertainty": CONTRACT_COMMAND,
        "note": (
            "every dB here is RELATIVE — this order's level minus the "
            "fundamental's at the same excitation frequency — because the corpus "
            "banks no SPL anywhere and an absolute distortion figure would be "
            "invented. Distortion is a function of drive, so each role carries "
            "the level it was read at; a figure quoted without its drive names "
            "nothing. Rows are per (capture, role) and are NOT merged across "
            "captures, because captures are poses. Read a ratio peak against "
            "fundamental_re_band_median_db before believing it: the ratio rises "
            "wherever the fundamental dips, with no change in harmonic energy"
        ),
    }


#: The air temperature :data:`DEFAULT_SOUND_SPEED_M_S` is the conventional
#: figure for, in degrees Celsius. Published beside the distance because it is
#: the ASSUMPTION the conversion rests on and nothing here measures room
#: temperature. Dry air's speed of sound is ``331.3 + 0.606*T`` m/s with ``T``
#: in Celsius, so 343.0 is the figure at 19.3 °C and 20 °C the round number it
#: is quoted for — the 0.4 m/s between them is smaller than a 1 K error.
_SPEED_OF_SOUND_AIR_TEMPERATURE_C = 20.0

#: The two numbers a capture's gate banks beside ``gate_disclosure``, as they
#: are spelled on a POSITION row. This set exists so
#: :func:`_gate_numbers_reason` can ask whether a round's records carry the
#: fields at all, which is a different question from whether their values are
#: null.
#:
#: ``gate_entanglement_floor_hz`` is deliberately NOT here: this set decides
#: the accuracy budget's ``gate_leakage.available``, whose subject is what the
#: gate DID to the spectrum, and the room's floor survives a capture that gated
#: nothing at all.
_POSITION_GATE_NUMBER_FIELDS = frozenset({
    "gate_moved_rms_db",
    "gate_reflection_delay_ms",
})

#: The same two facts as :data:`_POSITION_GATE_NUMBER_FIELDS`, as
#: :func:`~.capture_dispatch._gate_record` spells them inside ``verify.gate``:
#: the ``gate_`` prefix is dropped because the block is already the gate.
_VERIFY_GATE_NUMBER_FIELDS = frozenset({
    "moved_rms_db",
    "reflection_delay_ms",
})

#: Where the reflector-path conversion reads its delay from.
_REFLECTOR_PATH_SOURCE = "cloud_verify.json -> null_registry.tau_ladder_us"

#: Decimal places the reflector path length is published to — millimetres. A
#: millimetre of excess path is 2.9 us of delay, already finer than anything
#: this number supports: the fitted ladder tau and the directly measured
#: arrival tau disagree by up to 7.5 % on the S0 corpus (about 22 us, or 8 mm,
#: at the ~300 us those taus were), and the assumed speed of sound moves the
#: answer 1.8 % over a 10 K room.
_REFLECTOR_PATH_DECIMALS = 3






def _gate_numbers_present(
    rows: list[dict[str, Any]], gate: dict[str, Any]
) -> bool:
    """Does ANY banked record in the round carry a gate number?

    Over both carriers — the cloud's position rows and ``verify.gate`` —
    because either one answering settles it. The ONE spelling of the
    question :func:`_gate_numbers_reason` answers "no" to and the accuracy
    budget's ``gate_leakage.available`` answers "yes" to.
    """
    return any(_POSITION_GATE_NUMBER_FIELDS & set(row) for row in rows) or bool(
        _VERIFY_GATE_NUMBER_FIELDS & set(gate)
    )


def _gate_numbers_reason(
    positions: dict[str, Any], verify: dict[str, Any]
) -> str:
    """Why this round carries no gate numbers, or ``""`` when it does.

    The sentence names both readings because the two carriers have different
    absence rules: ``verify.gate`` always spells both keys, null or not, while
    a position row is filtered by
    :data:`~jasper.attribution.position_evidence._RECORD_FIELDS`, which drops a
    ``None``. So it states what is checkable and names ``gate_floor_source`` as
    the field separating "banked before the writers existed" from "every
    capture was ungateable".

    Silent when nothing COULD have carried them: those absences are already
    reported by their own blocks.
    """
    rows = [row for row in positions.get("positions") or [] if isinstance(row, dict)]
    gate = verify.get("gate")
    gate = gate if isinstance(gate, dict) else {}
    if not rows and not gate:
        return ""
    if _gate_numbers_present(rows, gate):
        return ""
    return (
        "no banked record in this round carries gate_moved_rms_db or "
        "gate_reflection_delay_ms, so its gate survives as a sentence only, and "
        "neither number can be recovered from that prose without parsing it — "
        "which this packet will not do. Two different rounds look like this and "
        "positions[].gate_floor_source separates them: one banked before the "
        "capture-time writers gained the fields, and one every capture of which "
        "was ungateable, where there was never a number to bank"
    )


def _reflections_block(cloud: dict[str, Any], reason: str) -> dict[str, Any]:
    """How far the delayed copy travelled — the ladder's tau, converted.

    ``reflector_path_distance_m = tau_ladder_us * 1e-6 * c``, the whole
    computation: tau is ALREADY banked as
    ``honesty_mask.null_registry.tau_ladder_us``, and what was missing was the
    multiply.

    The LADDER's tau, not the arrival's: ``arrival_tau_us`` sits beside it on
    the same registry and still carries whatever a sub-minimum cluster held on
    a ``no_corroborating_arrivals`` refusal, so a distance built from it could
    be published from evidence the gate refused. The ladder's tau exists only
    after a frequency-domain and a time-domain estimator agreed within
    :data:`~jasper.audio_measurement.interference_nulls.LADDER_ARRIVAL_TOLERANCE`.

    Refuses BY NAME rather than publishing a zero: ``tau_ladder_us`` is 0.0
    when no ladder was fitted, and 0.0 metres would put the reflector at the
    microphone.
    """
    registry = _mapping(cloud.get("null_registry"))
    constants: dict[str, Any] = {
        "speed_of_sound_m_s": DEFAULT_SOUND_SPEED_M_S,
        "speed_of_sound_air_temperature_c": _SPEED_OF_SOUND_AIR_TEMPERATURE_C,
        "source": _REFLECTOR_PATH_SOURCE,
        "uncertainty": CONTRACT_COMMAND,
    }
    refusal = ""
    if not registry:
        refusal = (
            f"this round banked no interference-null registry ({reason}), so "
            "no fitted ladder delay exists to convert into a path length"
        )
    elif registry.get("reason"):
        refusal = (
            "the interference-null gate identified nothing in this round "
            f"(null_registry.reason={registry.get('reason')!r}), so its "
            "tau_ladder_us is the no-ladder sentinel rather than a delay"
        )
    tau_us = finite_float(registry.get("tau_ladder_us")) if not refusal else None
    if not refusal and (tau_us is None or tau_us <= 0.0):
        refusal = (
            "the interference-null registry reported no usable fitted ladder "
            "delay, so there is nothing to convert"
        )
    # ``tau_us is None`` cannot be reached with an empty ``refusal`` — the arm
    # above sets one for exactly that case. It is here to narrow the type for
    # the multiply below, not as a second guard.
    if refusal or tau_us is None:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": refusal,
            "tau_ladder_us": None,
            "reflector_path_distance_m": None,
            **constants,
            "note": (
                "no distance is published here and none is implied: a reader "
                "should not read the absent field as 'the reflector is close'"
            ),
        }
    return {
        "available": True,
        "tau_ladder_us": tau_us,
        "reflector_path_distance_m": round(
            tau_us * 1e-6 * DEFAULT_SOUND_SPEED_M_S, _REFLECTOR_PATH_DECIMALS
        ),
        **constants,
        "note": (
            "an EXCESS path length: how much further the delayed copy "
            "travelled than the direct sound, not a distance to a surface. "
            "Halving it for a mirror-image bounce is the reader's call and "
            "needs geometry this round does not bank. The per-capture gate "
            "delays on the positions rows are a DIFFERENT tau — one pose, one "
            "instrument, the time domain — and are published as times rather "
            "than converted, so two numbers about two reflectors cannot be "
            "read as one"
        ),
    }


def _read_candidate(round_dir: Path) -> dict[str, Any]:
    """One round's own ``candidate.json``, as a plain mapping, or ``{}``.

    Read the light way every other banked artifact here is — JSON in, fields
    by name — never through ``MeasuredCrossoverCandidate.from_mapping``'s
    validation and fingerprint recompute. This packet re-verifies no
    artifact's integrity; that check lives in
    ``candidate_bank.load_candidate_artifact``.
    """
    raw, _reason = _read_json(round_dir / "candidate.json")
    return _mapping(raw)


def _unmeasured_repeat_floor(absence: str, reason: str) -> dict[str, Any]:
    """The shared shape for every absence — thresholds falling back to the two
    ``round_evidence`` constants that self-describe as assumptions. ``absence``
    is the closed vocabulary a reader keys on; ``reason`` is for a human."""
    return {
        "kind": UNCERTAINTY_RANDOM,
        "available": False,
        "absence": absence,
        "reason": reason,
        "thresholds": {
            "source": "codified_assumption",
            "margin_db": MEASURED_BENEFIT_MARGIN_DB,
            "plateau_db": ITERATION_PLATEAU_DB,
            "note": (
                "both self-described assumptions in round_evidence.py, "
                "awaiting exactly this measurement"
            ),
        },
    }


#: Why the repeat floor is not available: never measured, a file that is not
#: a readable record, or a record whose aggregate row cannot yield thresholds.
#: Three different errands (run E2 / re-copy the file / re-bank it), so the
#: packet names which rather than one shared reason.
REPEAT_FLOOR_UNMEASURED = "unmeasured"
REPEAT_FLOOR_UNREADABLE = "unreadable"
REPEAT_FLOOR_UNUSABLE = "unusable"


def _repeat_floor_source(path: Path | None) -> tuple[dict[str, Any] | None, str]:
    """The banked floor, or why there is none — ``source_absent`` when no file
    was there to read, the read failure otherwise (same rule as
    :func:`applied_profile_source`)."""
    if path is None:
        return None, "source_absent"
    record = load_repeat_floor(state_path=path)
    if record is not None:
        return record, ""
    _, reason = _read_json(path)
    return None, reason or f"not a {REPEAT_FLOOR_KIND} record"


def _repeat_floor_component(
    record: dict[str, Any] | None, read_reason: str
) -> dict[str, Any]:
    """The RANDOM repeat floor as banked, or one of three honest absences."""
    if record is None and read_reason == "source_absent":
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNMEASURED,
            "unmeasured -- no banked repeat floor; calibration experiment "
            "E2 (N touched-nothing fixed-pose repeat rounds through "
            "jasper-round-views repeat-floor; "
            "Calibration experiments)",
        )
    if record is None:
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNREADABLE,
            f"banked repeat floor could not be read ({read_reason}); re-copy "
            "it, or re-bank it with jasper-round-views repeat-floor",
        )
    thresholds = stopping_thresholds(record)
    if thresholds is None:
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNUSABLE,
            f"banked repeat floor carries no usable {record.get('aggregate_metric')} "
            "row (a finite, positive pairwise_abs_delta_p95_db and a finite "
            "pairwise_abs_delta_median_db); re-bank it with "
            "jasper-round-views repeat-floor",
        )
    rows = [row for row in record.get("rounds") or [] if isinstance(row, Mapping)]
    return {
        "kind": UNCERTAINTY_RANDOM,
        "available": True,
        "absence": None,
        "source": (
            "repeat-floor.json (jts_active_speaker_repeat_floor, written by "
            "jasper-round-views repeat-floor)"
        ),
        "n_repeats": record.get("n_repeats"),
        "measured_at": record.get("measured_at"),
        "bundle_session_ids": [row.get("bundle_session_id") for row in rows],
        "graph_fingerprints": sorted(
            {
                str(row["graph_fingerprint"])
                for row in rows
                if row.get("graph_fingerprint") is not None
            }
        ),
        "aggregate_metric": record.get("aggregate_metric"),
        "metrics": record.get("metrics"),
        "thresholds": {"source": "banked_repeat_floor", **thresholds},
        "reason": "",
    }


# :mod:`~jasper.active_speaker.linearization_envelope` is the ONE place the
# mic-tier trust ceiling is defined, so it is imported rather than restated.
# The table is private there because this is the only reader outside that
# module needing the raw breakpoints rather than the composed per-bin curve
# :func:`~.linearization_envelope.mic_trust_limit` returns.
def _accuracy_budget_block(
    *,
    positions: dict[str, Any],
    reflections: dict[str, Any],
    verify: dict[str, Any],
    round_dir: Path | None,
    repeat_floor: dict[str, Any] | None,
    repeat_floor_reason: str,
) -> dict[str, Any]:
    """Random beside systematic (ADR-0202) — juxtaposed, never pooled.

    Assembled from fields the packet/bundle already carries: nothing measured
    fresh, and no two figures ever added together, so a 0.04 dB repeat floor
    cannot read as accuracy beside a systematic bound that dwarfs it.

    Four components, each labelled its own kind and each honest about absence:

    * ``cross_seat_position_spread`` — UNSEPARATED, pointing at
      ``positions.cross_seat_sigma`` rather than re-embedding its array.
    * ``in_capture_repeat_floor`` — RANDOM, from the banked repeat floor
      (:mod:`jasper.active_speaker.repeat_floor`), ``available=False`` when
      the rig has none. Unmeasured, never defaulted to 0.0.
    * ``gate_leakage`` — SYSTEMATIC: a bias one capture's window bakes in, so
      more captures at the SAME pose do not shrink it.
    * ``mic_calibration_tier`` — SYSTEMATIC, PER ROLE off ``candidate.json``'s
      ``linearization[*].mic_tier``. Roles fitted under different tiers are
      published as the disagreement they are.

    No score, no recommendation, no verdict: this juxtaposes, an LLM judges.
    """
    from jasper.active_speaker.linearization_envelope import (
        MIC_TIERS,
        _MIC_TRUST_TABLE_HZ,
    )

    cross_seat = _mapping(positions.get("cross_seat_sigma"))
    cross_seat_available = bool(cross_seat.get("available"))

    rows = [row for row in positions.get("positions") or [] if isinstance(row, dict)]
    gate = _mapping(verify.get("gate"))
    gate_available = bool(reflections.get("available")) or _gate_numbers_present(
        rows, gate
    )

    candidate = _read_candidate(round_dir) if round_dir is not None else {}
    linearization = _mapping(candidate.get("linearization"))
    # Per role, never elected: two roles fitted under different tiers is a
    # fact this block discloses, not a tie one entry silently wins.
    tier_by_role = {
        str(role): str(entry["mic_tier"])
        for role, entry in linearization.items()
        if isinstance(entry, Mapping) and isinstance(entry.get("mic_tier"), str)
    }
    # dict.fromkeys, not set: dedupe with a run-stable order, since this
    # document is content-fingerprinted.
    trust_ceiling_hz_by_tier: dict[str, dict[str, float]] = {
        tier: {"full_to_hz": bp[0], "taper_zero_hz": bp[1]}
        for tier in dict.fromkeys(tier_by_role.values())
        if (bp := _MIC_TRUST_TABLE_HZ.get(tier)) is not None
    }

    return {
        "note": (
            "juxtaposes this round's RANDOM terms against the standing "
            "SYSTEMATIC bounds (ADR-0202); built from fields the "
            "packet/bundle already carries, nothing measured fresh and "
            "nothing pooled. Every component labels its OWN kind"
        ),
        "components": {
            "cross_seat_position_spread": {
                "kind": UNCERTAINTY_UNSEPARATED,
                "available": cross_seat_available,
                "n_seats": cross_seat.get("n_seats"),
                "source": "positions.cross_seat_sigma",
                "reason": (
                    "" if cross_seat_available
                    else str(cross_seat.get("reason") or "")
                ),
                "note": (
                    "the per-bin array is "
                    "positions.cross_seat_sigma.per_bin_sigma_db; not "
                    "duplicated here"
                ),
            },
            "in_capture_repeat_floor": _repeat_floor_component(
                repeat_floor, repeat_floor_reason
            ),
            "gate_leakage": {
                "kind": UNCERTAINTY_SYSTEMATIC,
                "available": gate_available,
                "source": (
                    "reflections.reflector_path_distance_m, "
                    "verify.gate.moved_rms_db, positions[].gate_moved_rms_db"
                ),
                "reason": (
                    "" if gate_available
                    else "no capture in this round carries a gate-disclosure "
                    "number"
                ),
            },
            "mic_calibration_tier": {
                "kind": UNCERTAINTY_SYSTEMATIC,
                "available": bool(tier_by_role),
                "tier_by_role": tier_by_role,
                "tier_vocabulary": list(MIC_TIERS),
                "trust_ceiling_hz_by_tier": trust_ceiling_hz_by_tier,
                "source": "candidate.json linearization[*].mic_tier",
                "reason": (
                    "" if tier_by_role
                    else "no banked candidate names a mic tier for this round"
                ),
            },
        },
    }


#: How many rounds of structural history are carried.
STRUCTURAL_HISTORY_MAX_ROUNDS = 8

#: How many recent bundles :func:`_structural_history_block` looks at before
#: giving up on finding :data:`STRUCTURAL_HISTORY_MAX_ROUNDS` rounds that
#: banked a candidate. Wider than the round count: commissioning and
#: calibration bundles carry no ``candidate.json`` and are skipped rather than
#: counted against the budget.
_STRUCTURAL_HISTORY_BUNDLE_SCAN_LIMIT = 32

#: EVERY structural axis a round re-derives, in report order — the axes the
#: three prescription classes exist to pin (:mod:`.driver_prescription`'s
#: ``pinned_trim_db``, :mod:`.alignment_prescription`'s ``delay_us`` and
#: ``polarity``, :mod:`.topology_prescription`'s corner). Declared once so the
#: history below is a loop: an axis named here appears on every row, and an
#: axis left out is a silent re-derivation.
STRUCTURAL_HISTORY_AXES: tuple[str, ...] = (
    "trim_db",
    "delay_us",
    "polarity",
    "crossover_fc_hz",
)


def _structural_axes_of(candidate: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """One candidate's committed value for each :data:`STRUCTURAL_HISTORY_AXES`.

    Every axis answers with the same two keys — ``value`` and ``pinned``
    (``None`` where the candidate banks no such bit) — so a reader walks the
    axes rather than learning a shape per axis.

    ONE frame per axis, never across artifacts: every value is read off
    ``candidate.json``, so ``polarity`` stays the candidate's own action word,
    a flip RELATIVE to the declared ``upper_polarity``. The applied profile's
    ABSOLUTE per-role ``inverted`` flags would put two rows in two frames on
    any speaker whose draft declares an inverted branch; that conversion's one
    owner is ``commanded.profile_graph_summation``.
    """
    linearization = _mapping(candidate.get("linearization"))
    alignment = _mapping(candidate.get("alignment"))
    analysis = _mapping(candidate.get("analysis"))
    region = next(
        iter(
            _mapping(candidate.get("source_preset")).get("crossover_regions") or ()
        ),
        None,
    )
    polarity = alignment.get("polarity")
    values: dict[str, tuple[Any, Any]] = {
        "trim_db": (
            {
                str(role): float(value)
                for role, value in _mapping(
                    candidate.get("role_attenuations_db")
                ).items()
                if finite_float(value) is not None
            },
            {
                str(role): bool(_mapping(entry).get("trim_pinned") is True)
                for role, entry in linearization.items()
            },
        ),
        "delay_us": (finite_float(alignment.get("delay_us")), None),
        "polarity": (
            polarity if isinstance(polarity, str) and polarity else None,
            (
                bool(analysis.get("polarity_pinned"))
                if "polarity_pinned" in analysis
                else None
            ),
        ),
        "crossover_fc_hz": (
            finite_float(_mapping(region).get("fc_hz")), None,
        ),
    }
    return {
        axis: {"value": values[axis][0], "pinned": values[axis][1]}
        for axis in STRUCTURAL_HISTORY_AXES
    }


def _structural_history_block(session_dir: Path) -> dict[str, Any]:
    """Candidate axes across recent live or banked rounds, oldest first."""

    try:
        bundles = recent_round_sessions(
            session_dir, limit=_STRUCTURAL_HISTORY_BUNDLE_SCAN_LIMIT
        )
    except OSError:
        bundles = []

    newest_first: list[dict[str, Any]] = []
    for bundle_dir in bundles:
        round_dir, _reason = round_artifact_dir(bundle_dir)
        if round_dir is None:
            continue
        candidate = _read_candidate(round_dir)
        if _mapping(candidate.get("analysis")).get("measurement_status") == "unmeasured":
            continue
        axes = _structural_axes_of(candidate)
        # Emptiness, not falsiness: a committed delay of exactly 0.0 µs and a
        # polarity of ``keep`` are both readings.
        if all(
            entry["value"] is None or entry["value"] == {}
            for entry in axes.values()
        ):
            continue
        newest_first.append({"round_id": round_dir.name, "axes": axes})
        if len(newest_first) >= STRUCTURAL_HISTORY_MAX_ROUNDS:
            break

    oldest_first = list(reversed(newest_first))
    return {
        "available": bool(oldest_first),
        "max_rounds": STRUCTURAL_HISTORY_MAX_ROUNDS,
        "axes": list(STRUCTURAL_HISTORY_AXES),
        "rounds_covered": len(oldest_first),
        "rounds": [
            {"ordinal": index + 1, **entry}
            for index, entry in enumerate(oldest_first)
        ],
        "source": (
            "candidate.json role_attenuations_db / alignment / source_preset "
            "corner, across recent live or banked rounds"
        ),
        "note": (
            "oldest first, so a monotonic walk reads left to right. Values "
            "only -- no drift verdict. Every row answers for every axis; a "
            "null is 'this candidate banks none', never a substituted "
            "default, and 'pinned': null is 'the candidate banks no pin bit "
            "for this axis'. polarity is the candidate's own action word, a "
            "flip relative to the DECLARED polarity, so two rows compare and "
            "neither states an absolute wiring. History legitimately starts "
            "wherever the household's retained bundles do; rounds_covered "
            "states how many this reading actually found, bounded at "
            "max_rounds"
        ),
    }


def _region_block(receipt: dict[str, Any], reason: str) -> tuple[dict[str, Any], bool]:
    """The crossover region a proposal must sit inside, and whether it exists.

    ``round_measurements.blend.band_hz`` is the VERIFY absolute claim's own
    band, which decision 10 also makes the region the blend correction is
    solved and graded over, so a prescription is checked against the band the
    deterministic solver was bounded by rather than a second derivation.
    """
    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    band = blend.get("band_hz")
    shape = band is None and blend.get("reason") == ABSOLUTE_NO_CROSSOVER_TOPOLOGY
    band_field = "round_measurements.blend.band_hz"
    absent = _absence(
        ABSOLUTE_NO_CROSSOVER_TOPOLOGY if shape else reason, band is not None, band_field
    )
    if absent:
        return {"available": False, **absent}, shape
    return {
        "available": True,
        "band_hz": band,
        "source": "round_receipt.round_measurements.blend.band_hz",
        "note": (
            "the VERIFY absolute claim's band, which is also the region the "
            "deterministic blend correction is solved and graded over"
        ),
    }, shape


def _incumbent_block(
    receipt: dict[str, Any],
    reason: str,
    profile: dict[str, Any] | None,
    profile_reason: str,
    state: Mapping[str, Any],
    statefile_path: Path | None,
) -> dict[str, Any]:
    """What the speaker is PLAYING — three records, two questions.

    The BLEND correction is recorded in two places and reported side by side
    rather than reconciled: the receipt's
    ``round_measurements.blend.incumbent`` (what the round said it derived
    from) and the applied profile's ``blend_correction`` (what the graph
    carried). They should agree, and reconciling them is a judgement this
    module does not make.

    ``linearization`` is the same question asked of the other prescription
    class, read through
    :func:`~jasper.active_speaker.baseline_profile.profile_linearization`,
    which owns WHICH copy of that field is authoritative.

    The applied-profile SSOT answers both halves and the flow state does not:
    what the flow state records names the graph live BEFORE the last v2 apply,
    so it is one apply behind after any v2 apply and arbitrarily behind after
    an apply through a door that never touches v2 state.

    That is load-bearing because a per-driver prescription is a TOTAL for every
    role it names, so a role's incumbent filters are DELETED by any document
    that names the role and does not repeat them.

    ``identity`` says WHICH profile the answer describes. ``config.path`` is
    not among its fields — the packet excludes absolute paths, and
    ``config.sha256`` names the same graph. Its ``applied_profile_displacement``
    is the question one layer up (#2537, #3316): is this record still what the
    speaker is PLAYING, answered against ``statefile_path`` — a CamillaDSP
    statefile banked at the SAME time as the profile, never a live read, so a
    packet rebuilt away from the box it describes reports what was true at
    bank time and not the reading machine's own state. ``None`` (no statefile
    supplied) reads as unknown, not as agreement.

    ``trim`` is a fourth record, LEVEL rather than shape: see
    :func:`_incumbent_trim_block`.
    """
    from jasper.active_speaker.baseline_profile import (
        applied_profile_displacement,
        profile_blend_correction,
        profile_linearization,
    )

    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    from_receipt = blend.get("incumbent")
    # ``profile_blend_correction`` and not an attribute read: it owns the same
    # snapshot-first authority rule ``profile_linearization`` owns, and it
    # keeps ``None`` (no readable profile) apart from ``()`` (a profile that
    # applied none) — the distinction this block's two consumers both need.
    from_profile = profile_blend_correction(profile)
    linearization = profile_linearization(profile)
    trim = _incumbent_trim_block(profile, state)
    return {
        "from_round_receipt": (
            from_receipt
            if from_receipt is not None
            else _absence(reason, False, "round_measurements.blend.incumbent")
        ),
        "from_applied_profile": (
            list(from_profile)
            if from_profile is not None
            # ``profile_blend_correction`` returns ``()`` for a profile that
            # applied no blend, so ``None`` beside a READABLE profile can only
            # be a malformed record — a different fact from a missing one, and
            # ``_absence``'s bare ``field_null`` would spell them the same.
            else _absence(
                profile_reason
                or "the profile is readable but its blend_correction is not a list",
                False,
                "applied_baseline_profile.blend_correction",
            )
        ),
        "identity": (
            {
                "baseline_id": profile.get("baseline_id"),
                "candidate_fingerprint": profile.get("candidate_fingerprint"),
                "applied_at": profile.get("applied_at"),
                "config_sha256": _mapping(profile.get("config")).get("sha256"),
                "applied_profile_displacement": (
                    applied_profile_displacement(
                        profile, statefile_path=statefile_path
                    )
                    if statefile_path is not None
                    else _absence(
                        "no CamillaDSP statefile was supplied",
                        False,
                        "camilla_statefile",
                    )
                ),
                "note": (
                    "which applied profile the filters below describe. A "
                    "packet built from a bank names the profile that was live "
                    "when the bank was pulled, not the one live now"
                ),
            }
            if profile
            else _absence(profile_reason, False, "applied_baseline_profile")
        ),
        "note": (
            "a prescription is a TOTAL, not a delta: prescribe the whole "
            "correction the next round should apply, incumbent included"
        ),
        "linearization": {
            # Keyed on the PROFILE, not on what it holds: an empty
            # linearization says the branches carry nothing, and only a
            # missing profile leaves the question unanswered. Filters are
            # copied VERBATIM — the profile stores exactly
            # `{biquad_type, freq, q, gain}`, and rounding would cost a reader
            # the ability to reproduce the cascade the speaker is playing.
            "from_applied_profile": (
                {
                    str(role): list(filters)
                    for role, filters in sorted(linearization.items())
                    if isinstance(role, str) and role.strip()
                }
                if profile
                else _absence(
                    profile_reason, False, "applied_baseline_profile.linearization"
                )
            ),
            "source": (
                "applied_baseline_profile.recomposition_snapshot.linearization, "
                "falling back to applied_baseline_profile.linearization"
            ),
            "note": (
                "the per-driver correction each branch is already carrying. A "
                "driver prescription is a TOTAL for every role it names: every "
                "filter listed here for a role you name and do not repeat is "
                "DELETED from the graph. A document may carry any filter listed "
                "here, shelves included, so repeat what you mean to keep. A "
                "shelf must LEAD its role's chain (or, a Highshelf taper, end "
                "it after a Lowshelf lead); the door refuses any other "
                "placement by name"
            ),
        },
        "trim": trim,
    }


def _incumbent_trim_block(
    profile: dict[str, Any] | None, state: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per role: what is APPLIED now, and what THIS round's own solve wants.

    ``applied_db`` reads the applied-profile SSOT's ``corrections`` (never the
    flow state's Undo stash — one apply behind, same as :func:`_incumbent_block`
    everywhere else). ``round_resolved_db`` reads the flow state's own
    ``candidate.trims_db`` — the trim this round's measurement produced —
    except for a role a prescription pinned this round, where that field holds
    the PIN rather than the solve it displaced; the solve is what
    ``candidate.trims_pinned[role].displaced_db`` banks instead
    (``durable_state._candidate_pinned_trims``).

    A role missing either half reports ``None``, never a substituted 0.0.
    """
    from jasper.active_speaker.baseline_profile import profile_driver_corrections

    applied = profile_driver_corrections(profile)
    candidate = _mapping(state.get("candidate"))
    resolved = _mapping(candidate.get("trims_db"))
    pinned = _mapping(candidate.get("trims_pinned"))
    out: dict[str, dict[str, Any]] = {}
    for role in sorted(set(applied) | set(resolved)):
        applied_db = finite_float(_mapping(applied.get(role)).get("gain_db"))
        resolved_db = (
            finite_float(_mapping(pinned[role]).get("displaced_db"))
            if role in pinned
            else finite_float(resolved.get(role))
        )
        out[role] = {
            "applied_db": applied_db,
            "round_resolved_db": resolved_db,
            "delta_db": (
                None if applied_db is None or resolved_db is None
                else resolved_db - applied_db
            ),
            "pinned_this_round": role in pinned,
        }
    return out


def _verify_block(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Per-claim verdicts, copied verbatim including their ``not_evaluated``.

    These live only in the flow state, never in the bundle — the receipt's
    ``verification`` block is a different, coarser record.
    """
    verify = state.get("verify")
    absent = _absence(reason, isinstance(verify, dict), "verify")
    if absent:
        return {"available": False, **absent}
    verify = _mapping(verify)
    return {
        "available": True,
        "outcome": verify.get("outcome"),
        "graded_band_hz": verify.get("graded_band_hz"),
        "claims": _mapping(verify.get("claims")),
        "gate": _mapping(verify.get("gate")),
        "delta_probe": _mapping(verify.get("delta_probe")),
    }


def _drivers_block(draft: dict[str, Any], reason: str) -> dict[str, Any]:
    """Each role's own declared band — the bound a per-driver filter sits inside.

    Read from the design draft's confirmed ``driver_safety_profile`` and
    composed by
    :func:`~.driver_prescription.driver_passbands_from_safety_profile`, so the
    packet reports a band it does not also define.

    Deliberately NOT derived from the crossover:
    ``branch_chain.radiating_band_hz`` is the band this driver is within 3 dB
    of full output over — the bound on a LIFT, narrower than the driver — and
    the whole driver is meant to be correctable.
    """
    profile = _mapping(draft.get("driver_safety_profile"))
    passbands = driver_passbands_from_safety_profile(profile)
    absent = _absence(reason, bool(passbands), "driver_safety_profile.targets")
    if absent:
        return {"available": False, **absent}
    return {
        "available": True,
        "passbands_hz": {
            role: [lo, hi] for role, (lo, hi) in sorted(passbands.items())
        },
        "source": (
            "design_draft.driver_safety_profile.targets[].measurement_band_hz, "
            "floored/capped by that target's own required_protection_filters"
        ),
        "confirmation": _mapping(profile.get("confirmation")),
        "note": (
            "the driver's published response range narrowed by whatever "
            "protective corners it declares. A per-driver prescription's "
            "filters must sit inside the band of the role they name"
        ),
    }


def _operator_notes_block(draft: dict[str, Any], reason: str) -> dict[str, Any]:
    """The operator's own words, passed through and read by nobody in code.

    The only block in this document that is neither measured nor gated.
    Composed by :func:`~.operator_notes.build_operator_notes` and embedded
    whole, with its own ``kind`` and schema version, so a reader can lift the
    prose out by kind and no evidence field ever carries a sentence.

    NOTHING in JTS reads these strings for a decision — no gate parses them, no
    bound derives from them, no branch tests them — which is why they can be
    carried verbatim with no length or content policy. Three tests hold that
    from three directions:
    ``test_the_prose_gatherer_has_exactly_one_production_caller``,
    ``test_no_shipped_module_reads_the_packets_operator_notes_block``, and
    ``test_prose_changes_nothing_in_the_packet_but_the_prose``.

    The one thing the prose does move is ``packet_fingerprint``, which is a
    content hash of the whole document rather than a reading of the prose.

    Absence takes the packet's ordinary two flavours; ``field_null`` is the
    ordinary case on a speaker whose operator typed nothing.
    """
    artifact = build_operator_notes(draft)
    absent = _absence(
        reason, bool(artifact["available"]), "design_draft.operator_prose"
    )
    return {**artifact, **absent} if absent else artifact


def _classification_block(raw: Any, reason: str) -> dict[str, Any]:
    """The banked feature verdicts and the working behind them, not re-derived.

    TWO views of one artifact, side by side and deliberately not joined.

    ``verdicts`` is the gate's, copied through
    :func:`~.feature_classification.read_feature_verdicts`, which drops a row
    it cannot type rather than admitting it as ``ambiguous``; ``n_rows_banked``
    beside it is the raw count.

    ``lab_rows`` is the artifact's own: every banked row, copied through
    :data:`~.feature_classification.LAB_ROW_FIELDS` with anything held back
    named in ``redacted_fields`` and any inexact column in
    ``non_finite_fields``. It carries the working a gate must not act on but a
    READER needs to audit a verdict. Rows the typed reader dropped keep their
    working here; they reach no gate, because no gate reads this key.

    Not joined into one list per feature: the typed reader drops rows, so the
    two do not line up by index, and pairing them by frequency is a judgement
    this module does not make. That is also why the two can disagree on a
    COLUMN — an artifact banked before the confidence vocabulary was
    normalised spells ``med`` where ``verdicts[]`` shows ``medium``.

    ``uncertainty`` labels every spread the rows publish, and says why the two
    columns that merely LOOK like uncertainties are not — ``gate_slack`` most
    of all, a dB bar beside a dB reading rather than an error bar on it.
    """
    absent = _absence(reason, raw is not None, CLASSIFICATION_ARTIFACT)
    if absent:
        return {
            "available": False,
            **absent,
            "note": (
                "no feature classification was banked for this round, so no "
                "per-driver filter of EITHER sign can be shown to be aimed at "
                "a minimum-phase driver defect rather than at an interference "
                "null or a room arrival"
            ),
        }
    verdicts = read_feature_verdicts(raw)
    banked = raw.get("rows") if isinstance(raw, dict) else raw
    lab_rows: list[dict[str, Any]] = []
    withheld: set[str] = set()
    non_finite: set[str] = set()
    for entry in banked if isinstance(banked, list) else []:
        if not isinstance(entry, dict):
            continue
        kept, dropped = _copy_allowed(entry, LAB_ROW_FIELDS)
        withheld.update(dropped)
        lab_rows.append({
            column: _exact_json_value(value, column, non_finite)
            for column, value in kept.items()
        })
    return {
        "available": bool(verdicts),
        "n_rows_banked": len(banked) if isinstance(banked, list) else 0,
        "n_rows_readable": len(verdicts),
        "verdicts": [verdict.to_dict() for verdict in verdicts],
        "lab_rows": lab_rows,
        "redacted_fields": sorted(withheld),
        "non_finite_fields": sorted(non_finite),
        "uncertainty": {
            "fields": {
                field: dict(entry)
                for field, entry in sorted(LAB_ROW_UNCERTAINTY.items())
            },
            "not_uncertainties": dict(sorted(LAB_ROW_NOT_AN_UNCERTAINTY.items())),
            "note": (
                "a random and a systematic uncertainty are never pooled into "
                "one number here. Each field above names its own kind: more "
                "captures shrink a random one and do not touch a systematic "
                "one, which is what a reader deciding whether to re-measure "
                "needs to know"
            ),
        },
        "source": CLASSIFICATION_ARTIFACT,
        "note": (
            "a 'defect-*' verdict says EQ is not structurally BARRED at that "
            "feature. It does not say EQ will help — the round that follows is "
            "what answers that, by measuring. verdicts[] is the gate's view "
            "and lab_rows[] is the artifact's own working behind it; the gate "
            "reads only the first"
        ),
    }


def _findings_block(round_dir: Path, cloud: dict[str, Any]) -> dict[str, Any]:
    """Every phase's banked finding set, keyed by phase, and the band bounding
    the two that are scanned for.

    ``present`` carries the distinction ``produced_by`` exists for: a banked
    set with an empty ``findings`` list RAN and promoted nothing. What its
    ABSENCE means is per phase — the cloud closes bank a set either way, while
    the level-frame gate banks one only when it promotes — and ``reason``
    separates a set banked here that this install could not read.

    ``echo_band_hz`` is the round's resolved echo-detector window and
    ``echo_band_bounds`` the phases it bounds (:data:`_ECHO_BAND_PHASES`), so
    an empty set is not read as a clean bill outside that band.
    """
    phases: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    field_descriptions: dict[str, Any] = {}
    for phase in _FINDING_PHASES:
        raw, reason = _read_json(round_dir / f"findings_{phase}.json")
        document = _mapping(raw)
        present = isinstance(raw, dict)
        if raw is not None and not present:
            reason = f"parsed as {type(raw).__name__}, not as a JSON object"
        rows = document.get("findings")
        rows = rows if isinstance(rows, list) else []
        phases[phase] = {
            "present": present,
            "produced_by": document.get("produced_by"),
            "reason": reason,
            "findings": rows,
        }
        counts[phase] = len(rows) if present else None
        # Per-SCHEMA and identical in every set, so one copy rather than three.
        field_descriptions = field_descriptions or _mapping(
            document.get("field_descriptions")
        )
    return {
        "summary": {
            "phases_present": [
                phase for phase in _FINDING_PHASES if phases[phase]["present"]
            ],
            "finding_count": counts,
            "echo_band_hz": cloud.get("echo_band_hz"),
            "echo_band_bounds": list(_ECHO_BAND_PHASES),
        },
        "phases": phases,
        "field_descriptions": field_descriptions,
    }


def _not_evaluated(
    *,
    receipt_reason: str,
    cloud_reason: str,
    state_reason: str,
    applied_profile_reason: str,
    classification_available: bool,
    drivers_available: bool,
    lateral_poses_available: bool,
    candidates_available: bool,
    capture_snr_reason: str,
    cross_seat_sigma_reason: str,
    harmonics_reason: str,
    gate_numbers_reason: str,
    reflector_path_reason: str,
    findings: dict[str, Any],
    no_crossover: bool,
) -> list[dict[str, Any]]:
    """Everything this packet could not answer, and why — one honest list.

    Entries whose absence is a property of the CORPUS are stated whether or not
    this particular session was complete.
    """
    entries: list[dict[str, Any]] = [
        # A property of the CORPUS — nothing in the package ANALYSES an
        # elevation, whoever wrote the artifact — so it belongs here rather
        # than as a per-row flag two producers spell differently. It
        # DISCLOSES and refuses nothing. The claim is about what this packet
        # READS, never about what a round banked.
        {
            "field": "vertical_plane_response",
            "reason": (
                CONTRACT_COMMAND
            ),
        },
    ]
    if not lateral_poses_available:
        # Narrow by construction: a lateral walk banks a signed whole-degree
        # bearing per pose, so the only true claim is about THIS round. It
        # speaks for no cloud seat either — those stamp their own
        # ``position_deg`` — and points at the block that does.
        entries.append({
            "field": "lateral_poses[].position_deg",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if not candidates_available:
        entries.append({
            "field": "candidates",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if gate_numbers_reason:
        # Names both numbers rather than only the reflection time: they are
        # banked together by ``spatial.cloud_position_record`` and
        # ``capture_dispatch._gate_record``, and they go missing together.
        entries.append({
            "field": "positions[].gate_reflection_delay_ms",
            "reason": gate_numbers_reason,
        })
    if reflector_path_reason:
        entries.append({
            "field": "reflections.reflector_path_distance_m",
            "reason": reflector_path_reason,
        })
    if capture_snr_reason:
        entries.append({
            "field": "capture_snr",
            "reason": capture_snr_reason,
        })
    if cross_seat_sigma_reason:
        entries.append({
            "field": "positions.cross_seat_sigma",
            "reason": cross_seat_sigma_reason,
        })
    if harmonics_reason:
        entries.append({
            "field": "harmonics",
            "reason": harmonics_reason,
        })
    if not classification_available:
        entries.append({
            "field": "per_bin_minimum_phase_class",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if not drivers_available:
        entries.append({
            "field": "drivers.passbands_hz",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if receipt_reason:
        entries.append({"field": "round_receipt", "reason": receipt_reason})
    if cloud_reason:
        entries.append({"field": "cloud_verify", "reason": cloud_reason})
    if state_reason:
        entries.append({
            "field": "flow_state",
            "reason": f"{state_reason}; per-claim verify verdicts live only here",
        })
    if applied_profile_reason:
        entries.append({
            "field": "incumbent",
            "reason": (
                f"{applied_profile_reason}; without the applied-profile SSOT "
                "this packet cannot name the correction the graph is already "
                "carrying, so a per-driver prescription's displacement is "
                "unknown rather than zero"
            ),
        })
    summary = _mapping(findings.get("summary"))
    if not any(_mapping(summary.get("finding_count")).values()):
        entries.append({
            "field": "findings",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if no_crossover:
        entries.append({"field": "crossover_region.band_hz", "reason": ABSOLUTE_NO_CROSSOVER_TOPOLOGY})
        entries.append({"field": "request_time_prescriptions.alignment", "reason": doors.ALIGNMENT_NO_CROSSOVER_REGION})
        entries.append({"field": "request_time_prescriptions.topology", "reason": doors.TOPOLOGY_NO_CROSSOVER_REGION})
    return entries


def build_crossover_evidence_packet(
    session_dir: Path,
    *,
    state_path: Path | None = None,
    driver_draft_path: Path | None = None,
    applied_profile_path: Path | None = None,
    repeat_floor_path: Path | None = None,
    declared_geometry_path: Path | None = None,
    statefile_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one round's banked evidence into one versioned document.

    ``session_dir`` is a commissioning bundle: an ``info.json`` beside an
    ``evidence/v1/artifacts/crossover_v2/<capture-session-id>/`` directory
    holding the round receipt, the cloud evidence, each phase's finding set
    and the per-position records.

    Every other path is OPTIONAL and INJECTED rather than resolved here — this
    packet is rebuilt by every reader, and a path resolved here would make a
    banked round's answer, and its ``packet_fingerprint``, depend on whatever
    the READING machine happens to have. Each absence is reported in the
    ``not_evaluated`` block rather than papered over, and each costs the packet
    something specific:

    * ``state_path`` — the flow state file (``jts_crossover_v2_flow_state``),
      banked outside the bundle; without it, no per-claim verify verdicts and
      no Fc selection.
    * ``applied_profile_path`` — the applied-baseline-profile SSOT, which
      answers "what is this speaker playing" for ``incumbent``; without it a
      per-driver prescription's displacement is ``unknown`` rather than
      guessed. See :func:`_incumbent_block` for why the flow state cannot
      stand in for it.
    * ``driver_draft_path`` — the design draft carrying the confirmed
      driver-safety profile; without it the per-driver prescription class has
      no bound to check against and refuses by name.
    * ``repeat_floor_path`` — the banked repeat floor; without it the floor is
      unmeasured and the two codified assumptions are used, named.
    * ``declared_geometry_path`` — the household's declared rig geometry, the
      only viable source for the room's entanglement floor.
    * ``statefile_path`` — a CamillaDSP durable statefile banked alongside
      ``applied_profile_path``; without it ``incumbent.identity``'s
      ``applied_profile_displacement`` (#2537, #3316) is unknown rather than a
      live read of whatever statefile the reading machine happens to have.

    Raises :class:`CrossoverEvidencePacketError` only when ``session_dir`` is
    not a crossover-v2 session bundle at all: a partially banked round is a
    normal thing to want to read.
    """
    if not session_dir.is_dir():
        raise CrossoverEvidencePacketError(f"not a directory: {session_dir}")
    info_raw, info_reason = _read_json(session_dir / "info.json")
    if not isinstance(info_raw, dict):
        raise CrossoverEvidencePacketError(
            f"bundle missing a readable info.json ({info_reason}): {session_dir}"
        )
    round_dir, round_reason = round_artifact_dir(session_dir)
    if round_dir is None:
        raise CrossoverEvidencePacketError(f"{round_reason}: {session_dir}")

    receipt_raw, receipt_reason = _read_json(round_dir / "round_receipt.json")
    cloud_raw, cloud_reason = _read_json(round_dir / "cloud_verify.json")
    classification_raw, classification_reason = _read_json(
        round_dir / CLASSIFICATION_ARTIFACT
    )
    harmonics_raw, harmonics_reason = _read_json(round_dir / HARMONICS_ARTIFACT)
    receipt = _mapping(receipt_raw)
    cloud = _mapping(cloud_raw)
    findings = _findings_block(round_dir, cloud)

    state_raw: Any = None
    state_reason = "no flow state file was supplied"
    if state_path is not None:
        state_raw, read_reason = _read_json(state_path)
        state_reason = read_reason
    state = _mapping(state_raw)
    state_withheld = sorted(key for key in _STATE_WITHHELD if key in state)
    if state and not state_matches_capture(state, round_dir.name):
        state, state_reason = {}, STATE_SESSION_UNKNOWN

    applied_profile, applied_profile_reason = applied_profile_source(
        applied_profile_path
    )
    repeat_floor, repeat_floor_reason = _repeat_floor_source(repeat_floor_path)

    draft_raw: Any = None
    draft_reason = "no driver design draft was supplied"
    if driver_draft_path is not None:
        draft_raw, read_reason = _read_json(driver_draft_path)
        draft_reason = read_reason
    drivers = _drivers_block(_mapping(draft_raw), draft_reason)
    operator_notes = _operator_notes_block(_mapping(draft_raw), draft_reason)
    classification = _classification_block(classification_raw, classification_reason)
    harmonics = _harmonics_block(harmonics_raw, harmonics_reason)
    # ONE scan of the bundle's take files, narrowed per block below: every
    # ``bundle_measurements`` call reopens all of them.
    take_rows = bundle_measurements(session_dir)
    lateral_poses = _lateral_poses_block(session_dir, take_rows)
    candidates = _candidates_block(take_rows)
    entry_baseline = _entry_baseline_block(session_dir, take_rows)

    capture_snr = _capture_snr_block(session_dir, take_rows)

    identity, identity_withheld = _copy_allowed(
        _mapping(info_raw.get("fingerprints")), _IDENTITY_FIELDS
    )
    spec = _mapping(cloud.get("spec"))
    positions = _positions_block(cloud)
    cross_seat_sigma = _mapping(positions.get("cross_seat_sigma"))
    verify = _verify_block(state, state_reason)
    reflections = _reflections_block(cloud, cloud_reason)
    crossover_region, no_crossover = _region_block(receipt, receipt_reason)

    packet: dict[str, Any] = {
        "artifact_schema_version": PACKET_SCHEMA_VERSION,
        "kind": PACKET_KIND,
        "generated_by": GENERATED_BY,
        "privacy": {
            "raw_audio_excluded": True,
            "absolute_paths_excluded": True,
            "household_prose_excluded": True,
            # …and the operator's prose is CARRIED, in exactly one block, which
            # is named here rather than left for a reader to discover. The two
            # are different populations with different writers: household copy
            # is the correction flow's carve-out text and stays withheld below,
            # while this is a commissioning declaration the LLM is meant to
            # read. Naming the block is what keeps the sentence above honest —
            # a document that quietly grew prose would be a different document
            # wearing the same schema version.
            "operator_prose_quarantined_to": OPERATOR_NOTES_BLOCK,
            "operator_prose_kind": OPERATOR_NOTES_KIND,
            "secrets_excluded": True,
            "microphone_serials_excluded": True,
            "withheld_state_fields": state_withheld,
            "note": (
                "captures are referenced by wav_sha256, never by path or "
                "content"
            ),
        },
        "session": {
            "bundle_session_id": info_raw.get("session_id"),
            "capture_session_id": round_dir.name,
            "state": info_raw.get("state"),
            "started_at": info_raw.get("started_at"),
            "round_id": receipt.get("round_id"),
            "declared_geometry": _declared_geometry_block(declared_geometry_path),
            "note": (
                "bundle_session_id and capture_session_id are different id "
                "namespaces; the round artifacts are filed under the capture id"
            ),
        },
        "identity": {
            **identity,
            "placement": _mapping(info_raw.get("placement")),
            "redacted_fields": identity_withheld,
            "calibration": _mapping(_mapping(state.get("evidence")).get("calibration")),
        },
        "round": {
            "available": bool(receipt),
            "schema_version": receipt.get("schema_version"),
            "adoption": _mapping(receipt.get("adoption")),
            "verification": _mapping(receipt.get("verification")),
            "round_axes": _mapping(receipt.get("round_axes")),
            "round_measurements": _mapping(receipt.get("round_measurements")),
            "evidence_identities": _mapping(receipt.get("evidence_identities")),
            "proposal_fingerprint": receipt.get("proposal_fingerprint"),
            "proposal_fingerprint_kind": receipt.get("proposal_fingerprint_kind"),
            "entry_graph_fingerprint": receipt.get("entry_graph_fingerprint"),
            "applied_graph_fingerprint": receipt.get("applied_graph_fingerprint"),
            **_absence(receipt_reason, bool(receipt), "round_receipt.json"),
        },
        "crossover_region": crossover_region,
        "incumbent": _incumbent_block(
            receipt,
            receipt_reason,
            applied_profile,
            applied_profile_reason,
            state,
            statefile_path,
        ),
        # Verbatim, every one of them. `spec.bands[]` carries `evaluable`,
        # `n_excluded` and `graded_lo_hz`; `flatness` carries `n_excluded` and
        # `evaluable`. Those are the fields that say how much of the band the
        # number actually covers, and a packet that summarised over them would
        # be easier to misread than the tools that print them.
        "flatness": _mapping(cloud.get("flatness")),
        "spec": spec,
        "curve": _mapping(cloud.get("curve")),
        "positions": positions,
        # The bearings, beside the seats and never inside them: a lateral walk
        # pose and a cloud position are different captures that share only a
        # take-id convention.
        "lateral_poses": lateral_poses,
        # WHICH configuration each of those takes measured, when a round
        # cycled more than one at a pose. Beside the poses rather than inside
        # them: a pose is where the mic stood, a candidate is what played.
        "candidates": candidates,
        # The round's measured "before", beside the after rather than inside the
        # receipt: the receipt carries identities, this carries the curve.
        "entry_baseline": entry_baseline,
        "capture_snr": capture_snr,
        "honesty_mask": {
            "merged_excluded_bands_hz": cloud.get("merged_excluded_bands_hz"),
            "screen_excluded_bands_hz": cloud.get("screen_excluded_bands_hz"),
            "null_registry": _mapping(cloud.get("null_registry")),
            "null_registry_crossover_region": _mapping(
                cloud.get("null_registry_crossover_region")
            ),
            "carve_outs": cloud.get("carve_outs") or [],
            "geometry": _mapping(cloud.get("geometry")),
            "trusted_floor_hz": cloud.get("trusted_floor_hz"),
            "validity_floor_hz": cloud.get("validity_floor_hz"),
            "note": (
                "a bin the merged mask removed is not a bin a prescription may "
                "correct; the mask is the only structural protection against "
                "cutting an interference null"
            ),
        },
        "findings": findings,
        "verify": verify,
        # The round's reflection geometry as NUMBERS, and the one place the
        # per-capture gate numbers on the positions rows and inside
        # verify.gate are declared. Top-level rather than inside honesty_mask,
        # which is copied verbatim from the cloud artifact.
        "reflections": reflections,
        # This round's RANDOM terms beside the standing SYSTEMATIC bounds
        # (ADR-0202) — juxtaposed only, never pooled and never a score.
        "accuracy_budget": _accuracy_budget_block(
            positions=positions,
            reflections=reflections,
            verify=verify,
            round_dir=round_dir,
            repeat_floor=repeat_floor,
            repeat_floor_reason=repeat_floor_reason,
        ),
        # Every structural axis's recent per-round history, so a monotonic
        # walk (a re-solved trim drifting round over round) and a basin flip
        # (opposite polarity at an unchanged delay) are both readable
        # evidence. Values only; no drift verdict.
        "structural_history": _structural_history_block(session_dir),
        # The two per-DRIVER evidence blocks. They travel together because a
        # per-driver prescription needs both to be checked at all: the band
        # says where a filter may sit, the verdicts say what it may be aimed
        # at, and either alone answers half the question.
        "drivers": drivers,
        # The CONTEXT layer, fenced. Everything above is measured or gated;
        # this one block is what a human typed, and it carries its own kind so
        # it can be lifted whole rather than read as another evidence field.
        "operator_notes": operator_notes,
        "installation": installation_evidence(_mapping(draft_raw)),
        "feature_classification": classification,
        # The third offline reading of the same captures, beside the other two.
        # Per (capture, role) rather than per driver: a MEASURE capture is one
        # pose, and distortion read at two poses is two readings, not one.
        "harmonics": harmonics,
        "not_evaluated": _not_evaluated(
            receipt_reason=receipt_reason,
            cloud_reason=cloud_reason,
            state_reason=state_reason,
            applied_profile_reason=applied_profile_reason,
            classification_available=bool(classification.get("available")),
            drivers_available=bool(drivers.get("available")),
            lateral_poses_available=bool(lateral_poses.get("available")),
            candidates_available=bool(candidates.get("available")),
            capture_snr_reason=str(capture_snr.get("reason") or ""),
            cross_seat_sigma_reason=str(cross_seat_sigma.get("reason") or ""),
            harmonics_reason=str(harmonics.get("reason") or ""),
            gate_numbers_reason=_gate_numbers_reason(positions, verify),
            reflector_path_reason=str(reflections.get("reason") or ""),
            findings=findings,
            no_crossover=no_crossover,
        ),
        "contracts": contract_digests(prescription_contracts(**{
            **contract_sources(session_dir), "draft": _mapping(draft_raw),
            "receipt": receipt, "applied_profile": applied_profile or {},
        })),
        # …and the doors this one does NOT open: the other two arrive as
        # session-open request-body keys, not something ``stage`` can act on.
        "request_time_prescriptions": doors.request_time_prescriptions(no_crossover, _absence),
    }
    packet["packet_fingerprint"] = _fingerprint(packet)
    return packet


def _fingerprint(packet: dict[str, Any]) -> str:
    try:
        return json_fingerprint(
            {key: value for key, value in packet.items() if key != "packet_fingerprint"},
            field_name="evidence_packet",
        )
    except EvidenceIdentityError as exc:
        raise CrossoverEvidencePacketError(f"packet is not exact JSON data: {exc}") from exc


class PacketSchemaUnsupported(CrossoverEvidencePacketError):
    reason = "packet_schema_unsupported"


def validate_packet(packet: Any) -> dict[str, Any]:
    """Check the complete frozen input, without consulting mutable source files."""
    if (
        not isinstance(packet, dict)
        or packet.get("kind") != PACKET_KIND
        or packet.get("artifact_schema_version") != PACKET_SCHEMA_VERSION
    ):
        raise PacketSchemaUnsupported(PacketSchemaUnsupported.reason)
    if packet.get("packet_fingerprint") != _fingerprint(packet):
        raise CrossoverEvidencePacketError("evidence packet content does not match its fingerprint")
    return packet


# --- the readers the gate uses, so the packet owns its own shape ---


def packet_region_band_hz(packet: Any) -> tuple[float, float] | None:
    """The crossover region, or ``None`` when the packet does not carry one.

    A reader rather than an attribute access, so
    :mod:`.blend_prescription` never has to know where in the document the
    band lives. The packet owns its own layout; the gate asks it questions.
    """
    if not isinstance(packet, dict):
        return None
    region = packet.get("crossover_region")
    if not isinstance(region, dict) or not region.get("available"):
        return None
    band = region.get("band_hz")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (lo > 0.0 and hi > lo):
        return None
    return (lo, hi)


def packet_driver_passbands_hz(packet: Any) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, or ``{}`` when the packet carries none.

    A reader rather than an attribute access, on
    :func:`packet_region_band_hz`'s rule: the packet owns its own layout and
    :mod:`.driver_prescription` asks it questions.

    ``{}`` rather than ``None`` because the gate's answer is the same either
    way — :data:`~.driver_prescription.PASSBAND_UNAVAILABLE` — and a second
    empty value would be a second thing every caller has to test for.
    """
    if not isinstance(packet, dict):
        return {}
    drivers = packet.get("drivers")
    if not isinstance(drivers, dict) or not drivers.get("available"):
        return {}
    bands = drivers.get("passbands_hz")
    if not isinstance(bands, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for role, band in bands.items():
        if not isinstance(role, str) or not role.strip():
            continue
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            continue
        try:
            lo, hi = float(band[0]), float(band[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if lo > 0.0 and hi > lo:
            out[role.strip()] = (lo, hi)
    return out


def packet_incumbent_linearization(
    packet: Any,
) -> dict[str, tuple[dict[str, Any], ...]] | None:
    """The per-driver correction the graph is already carrying, or ``None``.

    A reader rather than an attribute access: the packet owns its own layout.

    ``None`` ("this packet does not say") and ``{}`` ("it says the graph
    carries none") are DIFFERENT and both callers must keep them apart — a
    document that replaces a role it cannot see is the defect this reader
    exists to expose.

    Strict, and it fails the WHOLE map rather than a filter: a partial read
    would understate the displacement, the one direction this number must never
    err in. Permitted biquad types are the emitter's own
    ``camilla_yaml.LINEARIZATION_BIQUAD_TYPES``, consumed rather than restated.

    Entries come back in the reduced ``{biquad_type, freq, q, gain}`` shape
    :func:`~jasper.active_speaker.branch_chain.chain_response` takes.
    """
    from jasper.active_speaker.camilla_yaml import LINEARIZATION_BIQUAD_TYPES

    if not isinstance(packet, dict):
        return None
    block = _mapping(packet.get("incumbent")).get("linearization")
    if not isinstance(block, dict):
        return None
    roles = block.get("from_applied_profile")
    if not isinstance(roles, dict):
        return None
    # The builder writes an ``_absence`` here when no profile reached it, and
    # that shape is checked by name rather than inferred from its contents —
    # ``_incumbent_record``'s rule, for the same reason: an absence and a role
    # map are both dicts, and telling them apart by duck-typing would make a
    # banked role called ``status`` change the answer.
    if roles.get("status") == "not_evaluated":
        return None
    out: dict[str, tuple[dict[str, Any], ...]] = {}
    for role, filters in roles.items():
        if not isinstance(role, str) or not role.strip():
            return None
        if isinstance(filters, (str, bytes)) or not isinstance(filters, list):
            return None
        entries: list[dict[str, Any]] = []
        for entry in filters:
            if not isinstance(entry, dict):
                return None
            if entry.get("biquad_type") not in LINEARIZATION_BIQUAD_TYPES:
                return None
            # Real numbers, NOT anything ``float()`` will coerce, and ``bool``
            # excluded because it is an ``int`` subclass — the same test
            # ``blend_filters_from_mapping`` applies, for the same reason: this
            # system writes floats, so a string here is by definition a record
            # something else wrote.
            numbers: list[float] = []
            for value in (entry.get("freq"), entry.get("q"), entry.get("gain")):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return None
                numbers.append(float(value))
            freq, q, gain = numbers
            if not all(map(math.isfinite, numbers)):
                return None
            if freq <= 0.0 or q <= 0.0:
                return None
            entries.append({
                "biquad_type": str(entry["biquad_type"]),
                "freq": freq,
                "q": q,
                "gain": gain,
            })
        out[role.strip()] = tuple(entries)
    return out


def packet_feature_classifications(packet: Any) -> tuple[FeatureVerdict, ...] | None:
    """The banked verdicts, or ``None`` when this round has none.

    ``None`` and ``()`` are DIFFERENT here and the gate treats them the same
    way on purpose: both refuse. They are kept apart anyway because the packet
    distinguishes "no artifact was banked" from "one was and no row in it could
    be typed", and collapsing them at the reader would throw away a fact the
    block above it went to the trouble of reporting.
    """
    if not isinstance(packet, dict):
        return None
    block = packet.get("feature_classification")
    if not isinstance(block, dict) or not block.get("available"):
        return None
    return read_feature_verdicts(block.get("verdicts"))


def packet_positional_evidence(
    packet: Any,
) -> tuple[list[dict[str, Any]], list[float], float] | None:
    """The per-position curves, their shared grid, and the flat reference.

    ``None`` when any of the three is missing — they are only meaningful
    together, and a boost judged against two of them would be judged against a
    reference that did not come from the same evaluation as the curves.
    """
    if not isinstance(packet, dict):
        return None
    positions = packet.get("positions")
    spec = packet.get("spec")
    if not isinstance(positions, dict) or not isinstance(spec, dict):
        return None
    rows = positions.get("positions")
    grid = (positions.get("curve_grid") or {}).get("freqs_hz")
    reference = spec.get("reference_db")
    if not isinstance(rows, list) or not rows:
        return None
    if not isinstance(grid, list) or not grid:
        return None
    if isinstance(reference, bool) or not isinstance(reference, (int, float)):
        return None
    # `reference` is coerced inside the same guard as the grid: an
    # arbitrary-precision int passes the isinstance check above and then
    # raises on `float()`, so leaving it outside would reintroduce the escape
    # this guard exists to close.
    try:
        freqs = [float(value) for value in grid]
        reference_db = float(reference)
    except (TypeError, ValueError, OverflowError):
        return None
    return ([row for row in rows if isinstance(row, dict)], freqs, reference_db)
