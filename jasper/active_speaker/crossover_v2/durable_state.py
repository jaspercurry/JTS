# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The durable crossover-v2 state document: what it contains, both ways.

One file on the speaker holds everything a tuning round has to survive a
process restart, a page reload and the gap between two sessions. Until wave 3
rank 2 its schema existed only as a dict literal inside a web
handler — *"a schema writer with no schema"*
(``docs/REFACTOR-TUNING-2026-08.md`` §3). This module is that schema, and it
owns **both directions**: :func:`build_conductor_state` assembles the document,
and the ``*_from_state`` readers take it apart again.

**Both directions, deliberately.** ``docs/REFACTOR-TUNING-2026-08.md`` §1 makes
``analyze`` an offline verb — a banked session must be re-readable by an
analysis that did not exist when it was captured — and
:class:`~.session_seams.RecordStore` pairs ``persist`` with ``read_state`` for
exactly that reason. A writer whose readers lived somewhere else would make one
document have two owners, which is how the keys drift.

**What is NOT here: the file.** The host owns where the state lives, when it is
written and how durably —
``jasper.web.correction_crossover_v2``'s ``load_v2_state`` / ``save_v2_state``
keep the path, the schema version, the atomic write and the fsync decision.
This module never opens anything, which is what lets a test build a document
without a filesystem and what will let the engine's record store write one
without a web host. :func:`build_conductor_state` returns the document and the
durability verdict together (:class:`ConductorState`) rather than performing
the write, the same "return the decision, let the owner act" shape
:class:`~.spatial.CloudCombine` uses for its journal line.

**The conductor is duck-typed on purpose.** Several callers persist a stand-in
that implements only the fields their own assertion is about, so every read of
an optional field goes through ``getattr`` with a default. An absent attribute
means what the key's own absence already means downstream; it is never an
error.

**The carry-forward rules are the interesting half.** A key is either written
fresh by this session or inherited from the state being replaced, and *which*
is a per-key judgement with an incident behind it. Three shapes recur, and the
comment on each rule says which one it is and why:

* **unconditional** — the host-owned apply keys (``pre_apply_profile``,
  ``expected_post_apply_offset_db``, ``sound_declaration_undo``,
  ``round_anchor``). The deferred VERIFY that auto-arms after every apply runs
  under a brand-new relay session id, so a session-scoped guard drops them on
  the first post-apply write. Three separate P0s were caused by adding a
  host-owned key without a line here;
  ``test_every_host_owned_apply_key_survives_persist_conductor_state`` derives
  the set mechanically and fails on the fourth.
* **session-scoped** — ``applied``, ``candidate``, ``evidence``,
  ``apply_blocked``. A previous session's answer says nothing about this one.
* **phase-gated** — ``cloud`` and the ``cloud_artifacts`` refs on the group
  phases, ``pilot_transfer_reference`` / ``accepted_sound_revision`` /
  ``measure`` / the household-findings projection on ``PHASE_MEASURE``. Keyed
  on the phase that actually PRODUCES the value, so a session that never had
  the chance to produce one inherits rather than erases, and a session that did
  writes its own answer — empty included.

Dependency direction, as for every module in this package: no ``jasper.web``
import, and nothing from ``jasper.active_speaker.crossover_v2_flow``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jasper.log_event import log_event

from .round_anchor import ROUND_ANCHOR_STATE_KEY
from .topology_prescription import candidate_topology

logger = logging.getLogger(__name__)

__all__ = [
    "FINDING_HOUSEHOLD_REFS_KEY",
    "MAX_PERSISTED_SUM_POINTS",
    "ConductorState",
    "alignment_prescription_prior_from_state",
    "blend_prescription_prior_from_state",
    "blend_prescription_sha256_from_state",
    "build_conductor_state",
    "commanded_delta_prior_from_state",
    "declared_transfer_prior_from_state",
    "entry_baseline_prior_from_state",
    "pilot_transfer_prior_from_state",
    "topology_prescription_prior_from_state",
    "verify_measured_curve_from_state",
]


@dataclass(frozen=True)
class ConductorState:
    """One persist's answer: the document, and whether it must be fsynced.

    ``durable`` is true exactly when this write records a NEW round-receipt
    identity (#2291). A power cut that lost one would leave a receipt in the
    bundle that nothing points at — the "falsifies a receipt" half of
    ``save_v2_state``'s durability rule. Every other persist stays cheap, and
    there are many of them: one per capture.

    The verdict travels beside the document rather than being re-derived by the
    caller, because the fact that decides it (``round_receipt`` came from this
    conductor rather than from the prior) is only visible while the document is
    being built.
    """

    state: dict[str, Any]
    durable: bool



# Where the household-readable projection of a banked finding set rides inside
# the durable state's ``evidence`` refs: a list of ``{household_copy, at}``
# rows, written by :func:`_bank_household_findings` and read by
# :func:`_household_findings_status`. Up here with the other state vocabulary
# rather than beside its writer, because THREE places name it — the writer, the
# reader, and ``persist_conductor_state``'s carry-forward — and a key three
# functions spell is a name, not a local detail.
FINDING_HOUSEHOLD_REFS_KEY = "household_findings"

# Downsample ceiling for the persisted predicted-sum verify prior — enough
# resolution for the ±1.5 dB [Fc/2, 2Fc] comparison at 1/6-octave smoothing
# while keeping the durable state file small. Reduction to this ceiling is a
# block average in linear power (see ``_decimate_sum``), never a raw stride
# — issue #1858 found the prior stride picked one raw bin per output point,
# which aliases below ~600 Hz, where the 46.875 Hz stride spacing leaves
# fewer than 3 samples in a 1/3-octave band (below ~200 Hz the spacing
# exceeds the band's own width outright — zero guaranteed samples), so the
# "1/6-octave smoothing" this comment budgets resolution for was not
# actually happening upstream of it.
MAX_PERSISTED_SUM_POINTS = 512


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #


def _decimate_sum(predicted_sum: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the full-resolution predicted-sum curve to
    at most :data:`MAX_PERSISTED_SUM_POINTS` (the ``verify_priors.
    predicted_sum`` this module writes at :func:`persist_conductor_state`).

    **Issue #1858 — was a raw ``freqs[::step]`` stride, which aliases.**
    Picking one raw bin per output point keeps whichever bin the stride
    happened to land on; below ~600 Hz the 46.875 Hz stride spacing leaves
    fewer than 3 samples in a 1/3-octave band (below ~200 Hz it exceeds the
    band's own width outright — zero guaranteed samples), so the persisted
    LF shape was noise, not signal (point-to-point |Δ| median 0.39 dB vs.
    the properly-decimated cloud curve's 0.06 dB). Fixed by routing through
    the SAME block-average owner
    :func:`~jasper.active_speaker.crossover_v2_flow.spec_report_for_predicted_sum`
    already uses to grade this exact curve —
    :func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`
    — at this module's own ceiling rather than the analysis-grid one. That
    function block-averages in linear power (never subsamples), so every
    persisted point is a genuine local mean instead of one raw bin —
    generalizing the existing owner with a ``max_bins`` argument rather than
    adding a second decimation path.

    Era note: a state file persisted by a build before this fix carries
    whatever its own stride wrote; this only changes what a NEW persist
    writes; :func:`_predicted_spec_prior`'s stored verdict is untouched
    either way, since D4 grades the full-resolution tuple, never this
    decimated curve
    (see ``test_the_persisted_prediction_verdict_is_the_veto_s_not_a_re_grade``).
    **One path re-persists an old curve, not just new ones:** a verify-only
    re-arm rehydrates ``predicted_sum`` from whatever is on disk and feeds it
    straight back through this function at its own persist step, so a pre-fix
    513-point stride, encountered this
    way, is itself now block-averaged again — 513 → 256 at 93.75 Hz spacing
    (halved, not preserved) — where the old code would have left an
    already-persisted curve untouched on that path. Still honest values,
    just coarser than a fresh full-resolution persist would produce; the
    household's next MEASURE naturally replaces it.
    """
    if predicted_sum is None:
        return None
    freqs, mags = predicted_sum
    n = len(freqs)
    if n == 0:
        return None
    import numpy as np

    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    grid, curve_db = decimate_curve_to_analysis_grid(
        np.asarray(freqs, dtype=float), np.asarray(mags, dtype=float),
        max_bins=MAX_PERSISTED_SUM_POINTS,
    )
    return {
        "freqs_hz": [float(f) for f in grid],
        "magnitude_db": [float(m) for m in curve_db],
    }


def _decimate_delta(commanded_delta: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the COMMANDED delta (#2291 Phase 3).

    The delta probe's commanded axis —
    :func:`~jasper.active_speaker.crossover_v2_flow._commanded_delta`'s
    ``(freqs_hz, delta_db)``, the CHANGE the applied graph asks the speaker for
    relative to the graph it replaces — filters, role gains, polarity and delay
    (#2611). Bounded at the same :data:`MAX_PERSISTED_SUM_POINTS` ceiling,
    over the same fixed-width blocks, so it lands on the SAME grid
    :func:`_decimate_sum` produces for the same input frequencies — the two
    curves cross the stage bridge together and a reader comparing them should
    not have to ask whether their frequencies line up. That agreement is pinned
    (``test_the_commanded_delta_persists_on_the_same_grid_as_the_predicted_sum``)
    rather than asserted, because the block rule lives in
    ``_decimate_to_analysis_grid`` and is restated here.

    **Averaged in dB, not in linear power** — the one place this deliberately
    parts company with :func:`_decimate_sum`.

    The REASON first: over a block, the arithmetic mean is the unbiased
    estimator of a DIFFERENCE of dB curves, where the power mean is biased
    upward by Jensen's inequality. That bias grows with the WITHIN-BLOCK spread
    of the values and vanishes across a block the curve is flat over — so it is
    largest exactly where the commanded delta is steepest, and zero where the
    two estimators would have agreed anyway. ``_decimate_sum``'s power mean is
    the right estimator for its own input, a MAGNITUDE curve; it is the wrong
    one here.

    The measured bound second. Re-derived through the production owner
    (:func:`~jasper.active_speaker.linearization_fit.complex_correction_response`)
    over 200 cascades of realistic shape — a highshelf plus 4–11 peaking
    biquads at Q 0.7–6 plus a trim, on 1,024–8,192-bin grids: worst single
    block disagreement **1.60 dB**, and **5 of 100,762** persisted bins change
    side of the 0.5 dB
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_MIN_COMMANDED_DB`
    floor. So the domain choice does reach band membership and not merely a
    third decimal — rarely, and the bias is largest where the commanded value
    is largest, which is furthest from the floor.

    (The 0.27 dB this docstring used to quote was not reproducible against the
    production response owner and has been replaced by the figures above.)

    What it does NOT move is the graded error. The conductor reconstructs
    ``realized = (measured - predicted) + commanded``, so ``realized -
    commanded`` cancels this curve exactly; the decimation reaches band
    membership near the commanded floor and the reported ``gain_factor``.
    """
    if commanded_delta is None:
        return None
    import numpy as np

    freqs, delta = commanded_delta
    grid = np.asarray(freqs, dtype=float)
    values = np.asarray(delta, dtype=float)
    n = int(grid.size)
    # A length disagreement is not reachable from ``_commanded_delta`` (it
    # builds both arrays on one grid), but this function persists whatever a
    # conductor holds and a raise here would lose the WHOLE snapshot, not just
    # this key. Absent means "nothing commanded", which is already the honest
    # reading downstream.
    if n == 0 or int(values.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block
        grid = grid[:kept].reshape(blocks, block).mean(axis=1)
        values = values[:kept].reshape(blocks, block).mean(axis=1)
    return {
        "freqs_hz": [float(f) for f in grid],
        "delta_db": [float(d) for d in values],
    }


def _decimate_verify_measured(tracking_curve: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the VERIFY capture's graded curve pair (#2522).

    ``crossover_v2_flow.CrossoverV2Session.verify_tracking_curve`` —
    ``(freqs_hz, measured_db, predicted_db)``, the very pair the delta probe
    graded. Bounded at the same :data:`MAX_PERSISTED_SUM_POINTS` ceiling over
    the same fixed-width blocks as :func:`_decimate_delta`, so a reader
    comparing the two records is comparing comparably-spaced grids. This curve
    is on the VERIFY capture's grid rather than the MEASURE prediction's, so
    the two are not expected to land on the SAME frequencies; a re-grade
    interpolates the commanded axis onto this one, exactly as the live probe
    does.

    **Averaged in dB, and here that is not merely the unbiased choice — it is
    what makes the record re-gradable.** Block-averaging in dB is linear, so the
    difference of the two decimated curves is exactly the decimated difference,
    and ``measured − predicted`` is precisely the quantity
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe` grades
    (``realized − commanded`` cancels the commanded curve). A power mean, right
    for :func:`_decimate_sum`'s magnitude curve, would bias each side
    differently by Jensen's inequality and leave a residual that belongs to
    neither the speaker nor the model.

    Why persist at all: the commanded and predicted priors were durable and the
    MEASURED curve was not, so the 2026-08-14 remote ``model_error`` verdict
    could not be re-graded against a corrected instrument without another full
    hardware run (#2522). Same store, same lifecycle, no retention knob of its
    own — this dict is rebuilt on every persist exactly like its neighbours.

    ``None`` for an absent curve, a curve that is not a triple, an empty grid,
    or arrays whose lengths disagree. Absent means "not re-gradable offline",
    which is the honest reading and is what every state file written before this
    key shipped already says.
    """
    if tracking_curve is None:
        return None
    import numpy as np

    try:
        freqs, measured, predicted = tracking_curve
    except (TypeError, ValueError):
        return None
    grid = np.asarray(freqs, dtype=float)
    measured_db = np.asarray(measured, dtype=float)
    predicted_db = np.asarray(predicted, dtype=float)
    n = int(grid.size)
    if n == 0 or int(measured_db.size) != n or int(predicted_db.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block

        def _blocks(values):
            return values[:kept].reshape(blocks, block).mean(axis=1)

        grid, measured_db, predicted_db = (
            _blocks(grid), _blocks(measured_db), _blocks(predicted_db)
        )
    return {
        "freqs_hz": [float(f) for f in grid],
        "measured_db": [float(v) for v in measured_db],
        "predicted_db": [float(v) for v in predicted_db],
    }


def verify_measured_curve_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any, Any] | None:
    """The persisted VERIFY curve pair, ready to re-grade (#2522).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.verify_measured``, and the mirror of
    :func:`commanded_delta_prior_from_state`: durable state in, the
    ``(freqs_hz, measured_db, predicted_db)`` triple
    :meth:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session.
    _run_delta_probe` consumes out. With the persisted ``commanded_delta`` and
    the ``delta_probe`` record's own ``requested_band_hz`` /
    ``expected_offset_db``, that is everything a laptop-side re-grade needs.

    ``None`` covers the honest cases that mean one thing to a re-grade — there
    is no measured curve to grade: a state file written before this key shipped,
    a session that never reached VERIFY, and a record that is not the triple
    this build writes. A length disagreement is one of those and is checked
    here, for :func:`commanded_delta_prior_from_state`'s reason: three arrays
    that are not one curve would otherwise reach the classifier as a grid
    mismatch instead of an absence.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get("verify_measured") if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs = record.get("freqs_hz")
    measured = record.get("measured_db")
    predicted = record.get("predicted_db")
    if not freqs or not measured or not predicted:
        return None
    if not (len(freqs) == len(measured) == len(predicted)):
        log_event(
            logger, "correction.crossover_v2_verify_measured_malformed",
            level=logging.WARNING,
            n_freqs=len(freqs), n_measured=len(measured), n_predicted=len(predicted),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(measured, dtype=float),
        np.asarray(predicted, dtype=float),
    )


def _predicted_spec_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's stored prediction verdict, in the shape the durable
    state carries it (two-stage commission D4).

    ``getattr`` rather than attribute access for the same reason
    :func:`_cloud_summary` guards its own reads: this persistence helper is
    called from test seams with conductor doubles that carry only the surfaces
    a given test exercises, and a missing property must read as "no verdict",
    not raise mid-persist and lose the whole snapshot.
    """
    report = getattr(conductor, "measure_predicted_spec_report", None)
    return dict(report) if isinstance(report, Mapping) else None


def _entry_baseline_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's entry baseline as durable state carries it (#2291).

    ``getattr`` plus a duck-typed ``to_dict`` for :func:`_predicted_spec_prior`'s
    reason: this persistence helper is called with conductor doubles that carry
    only the surfaces a given test exercises, and a missing property must read
    as "no baseline", not raise mid-persist and lose the whole snapshot.
    """
    baseline = getattr(conductor, "measure_entry_baseline", None)
    to_dict = getattr(baseline, "to_dict", None)
    if not callable(to_dict):
        return None
    record = to_dict()
    return dict(record) if isinstance(record, Mapping) else None


def _round_receipt_identity(conductor: Any) -> dict[str, Any] | None:
    """The conductor's round-receipt identity, or ``None`` (#2291).

    ``getattr`` for :func:`_predicted_spec_prior`'s reason: this persistence
    helper runs against conductor doubles carrying only the surfaces a given
    test exercises, and a missing property must read as "no receipt", never
    raise mid-persist and lose the whole snapshot.
    """
    record = getattr(conductor, "round_receipt_identity", None)
    return dict(record) if isinstance(record, Mapping) else None


def entry_baseline_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 entry baseline, as the conductor's ctor takes it (#2291).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.entry_baseline`` and the exact mirror of
    :func:`commanded_delta_prior_from_state` below: durable state in, the
    ``measure_entry_baseline`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    Returns an
    :class:`~jasper.active_speaker.crossover_v2.round_evidence.EntryBaseline`
    or ``None``. ``None`` is
    :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_BASELINE_UNAVAILABLE`,
    which is INDETERMINATE and **not** a pass, and it covers the honest cases
    that mean one thing to the round — there is no comparable before: a state
    file written before this key shipped, a stage 1 whose baseline capture
    never landed, and a truncated or hand-edited record. Which of those it was
    is not recoverable from the file and the round does not branch on it.

    Shape validation belongs to ``EntryBaseline.from_dict``, which owns the
    "length-agreeing curve and mask, or nothing" rule.
    """
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    priors = (state or {}).get("verify_priors")
    record = priors.get("entry_baseline") if isinstance(priors, Mapping) else None
    return EntryBaseline.from_dict(record)


def alignment_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 delay prescription, as the conductor's ctor takes it (#2662).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.alignment_prescription`` and the exact mirror of
    :func:`entry_baseline_prior_from_state`: durable state in, the
    ``alignment_prescription`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    ``None`` is "this round prescribed no delay", and it also covers a state
    file written before this key shipped and a truncated or hand-edited record;
    the reader below says which of the last two in a WARNING, and the round
    does not branch on it. The BOUND is deliberately not re-applied here —
    :mod:`~jasper.active_speaker.crossover_v2.alignment_prescription` states
    why: it has one owner, and it is the request boundary.
    """
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        alignment_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("alignment_prescription") if isinstance(priors, Mapping) else None
    )
    return alignment_prescription_from_mapping(record)


def topology_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """Durable state in, the ``topology_prescription`` argument out.

    The exact mirror of :func:`alignment_prescription_prior_from_state`, module
    level for the same reason and reading the same durable ``verify_priors``
    block.  What it feeds is larger than a receipt field: the grading stage
    re-opens its session AT this topology, so a pin that failed to rehydrate
    would silently grade a pinned round's VERIFY against the crossover the
    speaker used to run.  The read-back is deliberately shape-only — see
    :func:`~jasper.active_speaker.crossover_v2.topology_prescription.topology_prescription_from_mapping`
    for why re-applying the bounds at grading time could only throw away the
    evidence of a round that really ran.
    """
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        topology_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("topology_prescription") if isinstance(priors, Mapping) else None
    )
    return topology_prescription_from_mapping(record)


def blend_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 blend prescription, as the conductor's ctor takes it (A9).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.blend_prescription``, and the exact mirror of
    :func:`alignment_prescription_prior_from_state` — durable state in, the
    ``blend_prescription`` argument out.

    **Without this arm the feature loses the thing it exists to bank.**
    ``verify_priors`` is rebuilt from the conductor on EVERY persist, and a
    stage-2 conductor holds no prescription — so stage 2 writes ``None`` over
    stage 1's record before the round receipt is ever written, and a round that
    ran a prescribed correction is banked as though its correction had been
    solved. That is the same stops-one-step-short shape as #2698: the value
    reaches the durable state and then nothing carries it the rest of the way.

    ``None`` is "this round prescribed no blend correction" — the automatic
    path — and it also covers a state file written before this key shipped and a
    truncated or hand-edited record. The BOUND is deliberately not re-applied:
    :mod:`~jasper.active_speaker.crossover_v2.blend_prescription` states why —
    it has one owner, and it is the boundary that accepted the document.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        blend_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("blend_prescription") if isinstance(priors, Mapping) else None
    )
    return blend_prescription_from_mapping(record)


def blend_prescription_sha256_from_state(state: Mapping[str, Any] | None) -> str:
    """The digest beside the record above, or ``""``.

    Read separately because it is banked separately — see the persist's own
    comment for why the digest cannot live inside the record it describes.
    """
    priors = (state or {}).get("verify_priors")
    digest = (
        priors.get("blend_prescription_sha256") if isinstance(priors, Mapping)
        else None
    )
    return str(digest or "") if isinstance(digest, str) else ""


def pilot_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The PREVIOUS session's G3 reference, as durable state carries it (#1927).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.pilot_transfer_reference`` — the mirror of
    :func:`_predicted_spec_prior` above, and the whole of what a verify-only
    re-arm seeds a fresh conductor's *history* with.

    Named and module-level so the seeding PATH is drivable in a test without a
    relay: durable state in, the ctor's ``verify_pilot_transfer_prior``
    argument out. Whether that argument can ever become a comparator is the
    conductor's own contract (it cannot — there is no longer an argument that
    seeds one).

    Shape checking beyond "is it a mapping" belongs to the conductor, which
    owns the "values plus a date, or nothing" rule.
    """
    priors = (state or {}).get("verify_priors")
    prior = priors.get("pilot_transfer_reference") if isinstance(priors, Mapping) else None
    return prior if isinstance(prior, Mapping) else None


def commanded_delta_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 commanded delta, as the conductor's ctor takes it (#2291).

    The read side of :func:`persist_conductor_state`'s
    ``verify_priors.commanded_delta`` and the exact mirror of
    :func:`pilot_transfer_prior_from_state` above: durable state in, the
    ``measure_commanded_delta`` argument out. Named and module-level for the
    same reason — the seeding PATH is then drivable in a test without a relay.

    ``None`` is the probe's
    :data:`~jasper.active_speaker.delta_probe.VERDICT_UNAVAILABLE`, which is
    **not** a pass, and it covers three honest cases that mean one thing to the
    probe — there is no commanded axis to grade against: a state file written
    before this key shipped, a trims-only candidate (which commands no shape at
    all), and a record that is not the pair this build writes.

    **A length disagreement is one of those, checked here (#2316 N3).** The two
    arrays are read separately, so a truncated or hand-edited record yields two
    valid arrays that are not a curve. Returning them would leave the two
    surfaces disagreeing in the journal: a verify-only re-arm's capability line
    would report the commanded delta PRESENT, and the probe's own warning would
    report it unavailable a moment later. Both degrade safely, but one of the
    two lines is false, and an operator reading the capability line has no way
    to know which. Refusing the pair here — rather than documenting the
    disagreement — makes the capability line true by construction, for the cost
    of one comparison.
    """
    return _delta_prior_from_state(state, "commanded_delta")


def declared_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 STATE axis, as the conductor's ctor takes it (#2614).

    The applied graph's own transfer against the uncorrected crossover — the
    axis the delta probe's two directional safety rules mask on. The exact twin
    of :func:`commanded_delta_prior_from_state` above, through the same reader,
    and it degrades the same way: ``None`` means the probe falls back to the
    CHANGE axis alone for those two rules, which is the pre-#2614 behaviour and
    an identity on a first-ever apply.
    """
    return _delta_prior_from_state(state, "declared_transfer")


def _delta_prior_from_state(
    state: Mapping[str, Any] | None, key: str,
) -> tuple[Any, Any] | None:
    """One ``verify_priors`` curve record, rehydrated — the shared reader.

    Both delta axes persist through :func:`_decimate_delta` and rehydrate
    through here, so the length check the docstring above argues for cannot end
    up applied to one axis and not the other.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get(key) if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs, delta = record.get("freqs_hz"), record.get("delta_db")
    if not freqs or not delta:
        return None
    if len(freqs) != len(delta):
        log_event(
            logger, "correction.crossover_v2_commanded_delta_malformed",
            level=logging.WARNING, prior=key,
            n_freqs=len(freqs), n_delta=len(delta),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(delta, dtype=float),
    )


def _finite(value: Any) -> float | None:
    """Mirrors ``crossover_envelope_v2._finite``'s guard (reject bool,
    reject non-numeric, reject NaN/inf) — N1 (2026-07-24 review follow-up):
    this module and that one stay symmetric about what counts as a
    displayable number, rather than one layer trusting a raw ``float(v)``
    the other layer would refuse. That symmetry is one-directional until
    #2470 lands: the ``OverflowError`` guard below is this function's
    alone, and the twin is exactly the trusting layer this note warns
    about until it mirrors the guard.

    Never raises. An unbounded JSON integer (``10 ** 400``) makes
    ``float()`` raise ``OverflowError`` rather than returning ``inf`` —
    this runs on every :func:`crossover_v2_status_block` read (the
    wizard's poll path), so an escaping conversion would be a 500 on a
    plain page load, the same hazard :func:`_household_findings_status`
    already guards against, and the same fallback (#2245).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _candidate_headroom_cost_db(linearization: Any) -> float:
    """The applied correction's disclosed max-level cost, dB (PR-L5).

    Thin adapter over the fit module's own reducer — defined there once so this
    browser payload and the conductor's cannot disagree about a
    household-facing number.
    """
    from jasper.active_speaker.linearization_fit import worst_headroom_cost_db

    if not isinstance(linearization, Mapping):
        return 0.0
    return worst_headroom_cost_db(linearization)


def _candidate_octave_summary(linearization: Any) -> dict[str, dict[str, float]]:
    """Gauge fix (2026-07-24): per-role OBSERVE-layer octave deficits
    (``LinearizationFit.observe_octave_summary`` — already computed by the
    fit engine, achieved-minus-target dB at each octave center), read
    straight off the live candidate's own rich ``linearization`` dict. Empty
    for a role whose fit never ran (ineligible/fit_failed/no fit) — nothing
    to disclose.

    A pure projection: this reads the fit's numbers and re-keys them, it has
    never derived a curve of its own. What the flat-linearization plan's PR-5
    changed is the FRAME these travel under — they are per-driver fit
    diagnostics from the design-axis capture, not the spec measurement, and
    ``crossover_envelope_v2._linearization_octave_rows`` (which renders them)
    carries the full reasoning."""
    out: dict[str, dict[str, float]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping):
            continue
        octaves = fit.get("observe_octave_summary")
        if not isinstance(octaves, Mapping) or not octaves:
            continue
        role_octaves: dict[str, float] = {}
        for hz, value in octaves.items():
            db = _finite(value)
            if db is not None:
                role_octaves[str(hz)] = db
        if role_octaves:
            out[str(role)] = role_octaves
    return out


def _candidate_octave_reasons(
    linearization: Any, octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, str]]:
    """Per-role octave-band reason codes (``LinearizationFit.reason_summary``
    — the fit engine's own closed
    :class:`~jasper.active_speaker.linearization_envelope.ReasonCode`
    vocabulary), the sibling of :func:`_candidate_octave_summary`'s numbers.
    Same pure projection, same band keying: the fit computes both dicts over
    the same octave centers in the same pass, so they line up band-for-band
    (see ``linearization_fit._observe_octave_summary``'s own docstring).

    Band-for-band, but NOT role-for-role on its own — which is why the
    already-projected ``octaves`` is an argument rather than something this
    recomputes. ``linearization_fit._empty_fit`` (the envelope allowed
    correction nowhere) returns an EMPTY ``observe_octave_summary`` beside a
    fully populated ``reason_summary``, so a role can honestly have verdicts
    and no numbers. Keying off the numbers makes the reason set a subset of
    the octave set by construction: no role is ever handed a verdict for a
    band this candidate has no number in.

    **Why the numbers need this (#2638).** ``observe_octave_summary`` is
    ``working_db - frame_target_db`` across the WHOLE grid. Above a driver's
    own radiating band the crossover target dives at 24 dB/oct while the
    measurement floor stays put, so the difference explodes into a large
    POSITIVE number — stopband arithmetic, not performance. On 2026-08-16 a
    healthy candidate's "+23.0 dB" at 16 kHz read on the review screen as a
    runaway boost and nearly indicted a correction whose largest filter gain
    anywhere was +2.5 dB. The fit engine already labels every one of those
    octaves ``envelope_out_of_band``; this hop is what carries the label to
    the surface that shows the number.

    A SEPARATE key rather than a compound value, deliberately: a candidate
    persisted before #2638 carries the numbers with no reasons, and an absent
    key reads as "this build did not record why" — which renders exactly as
    it did before, instead of making every reader handle two shapes of the
    same key.
    """
    out: dict[str, dict[str, str]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        reasons = fit.get("reason_summary")
        if not isinstance(reasons, Mapping) or not reasons:
            continue
        role_reasons = {
            str(hz): code for hz, code in reasons.items()
            if isinstance(code, str) and code
        }
        if role_reasons:
            out[str(role)] = role_reasons
    return out


def _candidate_octave_driver_classes(
    linearization: Any, octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, str]:
    """Per-role declared ``driver_class`` (``LinearizationFit.driver_class``),
    the third sibling of :func:`_candidate_octave_summary`'s numbers and
    :func:`_candidate_octave_reasons`'s verdicts (audit item 4i).

    Gated on the same ``octaves``-has-a-role-with-numbers membership as
    :func:`_candidate_octave_reasons`, for the identical reason: a role with
    no fit numbers has nothing on this screen for a driver_class value to
    annotate.

    A SEPARATE key rather than folded into ``_candidate_octave_reasons``'s
    dict, matching that function's own "separate key" rule: driver_class is a
    per-FIT scalar (one value for the whole role), not a per-band verdict, so
    a reader for one never has to parse the other's shape to find it. Needed
    because ``LIMITED_BY_CLASS_PRIOR`` fires for every declared class, not
    only the undeclared ("unknown") one — the remedy this exists for
    (crossover_envelope_v2._linearization_octave_rows) must tell an already-
    declared class apart from an undeclared one, since only the second has
    an action left to take.
    """
    out: dict[str, str] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        driver_class = fit.get("driver_class")
        if isinstance(driver_class, str) and driver_class:
            out[str(role)] = driver_class
    return out


def _candidate_pinned_trims(
    candidate: Any,
) -> dict[str, dict[str, float | None]]:
    """Each pinned role's shipped trim, the value it displaced, and the gap.

    Read off the candidate rather than taken from the session, on
    ``polarity_pinned``'s route: the pin is already frozen onto the artifact —
    ``build_candidate`` stamps ``trim_pinned`` and the ``displaced_trim_db`` it
    replaced onto the role's entry — so nothing here has to ask who chose it or
    re-derive what it moved.

    ``displaced_db``/``delta_db`` come from that banked ``displaced_trim_db``,
    which is the trim THIS round's lane actually solved for the role — exact on
    every lane. The program-analysis ``trim_db`` is deliberately NOT read: on the
    fitted lane it is the pre-commit number, a different value from the
    giveback-and-normalized trim the pin displaced, so a delta against it would
    misstate what the pin changed. ``None`` means the candidate carries no
    displaced value (a durable read-back of a pre-field artifact), never a
    substituted zero.
    """
    out: dict[str, dict[str, float | None]] = {}
    for role, entry in (candidate.linearization or {}).items():
        if not isinstance(entry, Mapping) or entry.get("trim_pinned") is not True:
            continue
        shipped = candidate.role_attenuations_db.get(str(role))
        if shipped is None:
            continue
        raw = entry.get("displaced_trim_db")
        displaced = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        out[str(role)] = {
            "pinned_db": float(shipped),
            "displaced_db": displaced,
            "delta_db": None if displaced is None else float(shipped) - displaced,
        }
    return out


def _candidate_summary(
    candidate: Any, *, topology_pinned: bool = False,
    headroom_cost_basis: str | None = None,
) -> dict[str, Any] | None:
    # Lazy, like ``_candidate_headroom_cost_db``'s own import below it: this
    # module has no module-level numpy and the fit module does, so the
    # socket-activated wizard process only pays for it on a path that
    # genuinely has a candidate.
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )

    # WHICH era stamped the per-branch charges below, supplied by the CALLER
    # because only the caller knows. The default is this build's era, true on
    # the minting path; a candidate read OFF DISK records none of its own, so
    # republish passes UNKNOWN rather than letting a pre-#2758 candidate wear a
    # current label over numbers the widened grid now charges more for.
    stamped_basis = headroom_cost_basis or HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN

    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    octaves = _candidate_octave_summary(candidate.linearization)
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        # …and which of those trims the round did NOT solve. Same rule as
        # ``crossover_pinned`` and ``polarity_pinned`` below: the household copy
        # must never word a pinned number as a measured result. The DISPLACED
        # value rides beside it — the trim this round would have shipped for the
        # role absent the pin — so a reader judging a pin sees the answer it
        # overrode. This discloses rather than blocks.
        "trims_pinned": _candidate_pinned_trims(candidate),
        # WHERE this candidate crosses, and whether the round was PINNED there.
        # The corner comes off the candidate, read by the module that owns the
        # shape; the bit comes from the session, because a corner cannot say
        # who chose it — and the household copy must never word a pinned corner
        # as a measured result, the same rule ``polarity_pinned`` below carries.
        "crossover": candidate_topology(candidate),
        "crossover_pinned": bool(topology_pinned),
        "alignment": candidate.alignment.to_dict(),
        # Threaded through for the conductor's own trust gate
        # (crossover_v2_flow.ALIGNMENT_CONFIDENCE_TRUST_FLOOR) and, on the
        # RESULT screen, the collapsed expert disclosure.
        "alignment_confidence": analysis.get("alignment_confidence"),
        # RESULT-screen expert disclosure only (crossover_envelope_v2
        # ._candidate_review_payload's "ripple_db").
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        # WHICH objective committed this candidate's (polarity, delay) pair, and
        # whether the committed delay left the comb lobe its physical anchor
        # owns (#2598, and the #2607 panel's S2/S3). The objective reaches the
        # review screen, which must not word a declared-design commitment as a
        # measured one; the lobe flag is a receipt line, because that mode is
        # magnitude-flat and an on-axis VERIFY cannot contradict it.
        "alignment_objective": analysis.get("alignment_objective"),
        # …and whether the polarity above was MEASURED or held by the request.
        # Its own key because the objective cannot say: a pinned round commits
        # the same `explicit_prescription_committed` an unpinned one does.
        "polarity_pinned": bool(analysis.get("polarity_pinned")),
        "left_anchor_lobe": analysis.get("left_anchor_lobe"),
        # Gauge fix (2026-07-24): WHY Layer-1a driver linearization did or
        # didn't run this attempt — "" / "fitted" / "trim_rejected" /
        # "ineligible_mic_tier" / "ineligible_repeats" / "fit_failed".
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        # Gauge fix (2026-07-24): per-role top-octave deficits (the number
        # that says "the top octave is 9 dB down and nothing corrected it").
        "linearization_octaves": octaves,
        # WHY each of those octaves reads the way it does (#2638). The number
        # alone cannot distinguish "the top octave is 9 dB down and nothing
        # corrected it" from "this octave is past the driver's own band, where
        # the difference is the crossover's rolloff rather than anything the
        # driver did." Both are honest; only one is about performance, and the
        # screen was showing the second as if it were the first.
        "linearization_octave_reasons": _candidate_octave_reasons(
            candidate.linearization, octaves
        ),
        # Which declared driver_class produced each role's octave verdicts
        # above (audit item 4i) — the fact
        # crossover_envelope_v2._linearization_octave_rows needs to tell an
        # already-declared class's own prior apart from the undeclared
        # ("unknown") default, so the remedy it attaches never tells a
        # household to redeclare a class it already named.
        "linearization_driver_class": _candidate_octave_driver_classes(
            candidate.linearization, octaves
        ),
        # "This correction costs N dB of maximum level" (linearization-integrity
        # PR-L5). The owner's ruling on boost is that headroom spend is
        # DISCLOSED, never silently limited — and a number that only reaches the
        # journal is not disclosed to the household that owns the speaker. This
        # is the DURABLE candidate block (``/state.crossover_v2.candidate``);
        # the envelope's screens read
        # ``crossover_envelope_v2._candidate_review_payload``, which projects
        # this field into ``headroom_cost`` alongside the era stamp below. So
        # persisting it here is the first half of making the ruling true — that
        # projection is the half a household actually sees.
        #
        # The WORST branch's charge, matching the emitter's own worst-branch
        # rule (``camilla_yaml.linearization_headroom_db``): the driver chains
        # run in parallel after the split, so the graph gives up the largest
        # branch's charge, not the sum across branches. (Each branch's charge
        # is its emitted chain's realized peak since #1808.) 0.0 for every cut-only
        # correction, which is every correction before PR-L5 — present and
        # zero rather than absent, so a surface never has to guess whether the
        # field is missing or the cost is nothing.
        "headroom_cost_db": _candidate_headroom_cost_db(candidate.linearization),
        # WHICH derivation the number above was stamped under (#1808 /
        # two-stage commission D3) — see ``stamped_basis`` above for why it
        # comes from the caller, ``linearization_fit.HEADROOM_COST_BASIS_*``
        # for why an era is recorded rather than sniffed, and
        # ``crossover_envelope_v2._candidate_review_payload`` for what an
        # unknown one renders as.
        "headroom_cost_basis": stamped_basis,
    }


def _cloud_summary(conductor: Any) -> dict[str, Any] | None:
    """Per-group geometry verdict + position ids, or ``None`` when no group ran.

    Reads the conductor's public group surfaces only, and tolerates a conductor
    double that has none (the persistence helper is called from test seams too).
    """
    from jasper.active_speaker.crossover_v2.journey import GROUP_PHASES

    try:
        session_phases = tuple(conductor.session_phases)
    except (AttributeError, TypeError):
        return None
    out: dict[str, Any] = {}
    for phase in session_phases:
        if phase not in GROUP_PHASES:
            continue
        geometry = conductor.group_geometry(phase)
        if geometry is None:
            continue
        out[phase] = {
            "geometry": geometry,
            # The SURVIVING take per position (id + attempt). A bare id list
            # would be ambiguous after a geometry retake, where two takes share
            # an id and only one is in the cloud; the attempt is what joins
            # this to the per-take evidence artifacts.
            "positions": list(conductor.group_position_takes(phase)),
            # PR-4: the honest-instrument pipeline result for this group —
            # merged mask, null registry, evaluated spec, geometry guidance
            # copy, decimated curve (assemble_cloud_group_result's own JSON
            # shape, so this is verbatim what the bundle artifact carries
            # too). ``None`` only if the conductor double has no such method
            # (a pre-PR-4 test seam) — never "the pipeline was fine".
            "pipeline": (
                conductor.group_cloud_result(phase)
                if hasattr(conductor, "group_cloud_result")
                else None
            ),
            # PR-7: the PRODUCING session's id, stamped once here (not
            # re-derived downstream) — the provenance marker
            # ``_compact_cloud_status`` needs to tell "measured in the
            # currently active session" apart from "carried forward from an
            # earlier one". Carried forward unconditionally by
            # ``persist_conductor_state``'s own carry-forward branch below,
            # which copies this whole per-phase dict verbatim — so a stamp
            # written here survives every re-arm that does not itself close
            # a fresh group, without a second write site. Guarded the same
            # way as ``pipeline`` above (review N-3): a conductor double
            # built only to exercise this function need not carry every
            # attribute a real one does, and ``_compact_cloud_status``
            # already treats a missing/non-string stamp as unknown
            # provenance, never a fabricated one.
            "session_id": (
                str(conductor.session_id) if hasattr(conductor, "session_id") else None
            ),
        }
    return out or None


def _delta_probe_summary(probe: Any) -> dict[str, Any]:
    """The delta probe's verdict, small enough to live in durable state (#1811).

    Everything a reader needs to judge a non-rollback finding — the two that by
    design produce no refusal and would otherwise be invisible outside the
    journal: what the verdict was, why, the level move the emitter declared, the
    one it could not account for, and (since #2521) the two terms of the frame
    it removed. The full map (per-bin errors, exceedance width, gain factor,
    spatial arm, both bands) stays on
    ``event=correction.crossover_v2_delta_probe`` — this is the durable summary,
    not a second copy of the record.

    The frame terms are here rather than only on the journal line for exactly
    the reason ``residual_offset_db`` is: ``frame_mismatch`` is a claim ABOUT
    those two numbers, so a durable record naming the verdict without them
    would say a level and a slope explained the finding while withholding what
    they were. ``None`` when no frame was fitted — never 0.0, which would read
    as "measured, and flat".

    ``frame_n_bins`` / ``frame_band_hz`` ride beside them because they are
    ``frame_fit``'s own stated defence against an ill-conditioned fit: two
    scalars fitted over a narrow quiet span can be large and mean nothing, and a
    reader judging this verdict needs to see how much was trusted. They cannot
    change the verdict — the gate only narrows — but they bound how much weight
    the two terms above can carry.

    ``entry_anchor_offset_db`` and the three ``quiet_*`` terms are here for the
    same reason one step further (#2533). ``residual_offset_db`` is now a level
    CHANGE rather than an absolute disagreement, so the record has to say what
    standing offset was subtracted to make it one — and ``None`` there means
    nothing was, which changes how the number should be read. The quiet terms
    bound the claim: ``uncommanded_level_shift_outside_probe_band`` is a verdict
    ABOUT how little of the graded band its evidence covered, so the covered band
    and the coverage travel with it. ``quiet_core_band_hz`` is deliberately not a
    second copy of ``frame_band_hz``: that one is the min/max, and this one is
    the interquartile span, which is the difference the verdict turns on.

    ``getattr`` throughout, and through the nested ``frame`` too: this runs
    against duck-typed probe stand-ins in tests, and an absent field is
    "unknown", never a raise that loses the whole snapshot.
    """
    frame = getattr(probe, "frame", None)
    return {
        "verdict": str(getattr(probe, "verdict", "") or ""),
        "reason": str(getattr(probe, "reason", "") or ""),
        # Whether the realized-energy half of the safety axis ran (series-2 D1).
        # Here for ``residual_offset_db``'s reason one step further: that number
        # needed the record to say what was subtracted to make it a change, and
        # this one needs the record to say whether the subtraction was possible
        # at all — a first-ever round takes the ``state_axis_only`` branch, so
        # its axis reports SAFE with that half unrun.
        #
        # A FORENSIC state key: no renderer reads it today (the done screen's
        # caveat keys on the probe's verdict). It is here because the round
        # receipt is write-once and this record is the LIVE one — the surface
        # ``/state``, the doctor and the done screen would each have to read it
        # from — so a fact only the receipt holds is a fact no live surface can
        # ever show.
        "safety_anchored": bool(getattr(probe, "safety_anchored", False)),
        "expected_offset_db": getattr(probe, "expected_offset_db", 0.0),
        "residual_offset_db": getattr(probe, "residual_offset_db", None),
        "entry_anchor_offset_db": getattr(probe, "entry_anchor_offset_db", None),
        "quiet_n_bins": getattr(probe, "quiet_n_bins", None),
        "quiet_core_band_hz": (
            list(core) if isinstance(
                core := getattr(probe, "quiet_core_band_hz", None), tuple
            ) else None
        ),
        "quiet_probe_coverage": getattr(probe, "quiet_probe_coverage", None),
        "frame_offset_db": getattr(frame, "offset_db", None),
        "frame_tilt_db_per_octave": getattr(frame, "tilt_db_per_octave", None),
        "frame_n_bins": getattr(frame, "n_bins", None),
        "frame_band_hz": (
            list(band) if isinstance(band := getattr(frame, "band_hz", None), tuple)
            else None
        ),
    }


def build_conductor_state(
    conductor: Any,
    prior: Mapping[str, Any],
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
) -> ConductorState:
    """The whole document one persist writes, over the one it is replacing.

    ``prior`` is the state currently on disk (``{}`` when there is none): the
    carry-forward rules below read it, and reading it is the whole reason this
    is a document builder rather than a projection of the conductor. The host
    loads it and writes the answer — this function touches no file.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure (issue #1820). They are FORENSICS, never household copy:
    the envelope renders ``failure["code"]`` through the reason registry and
    ignores this key. It exists so a support read of the state file can tell
    which of ``program_unplayable``'s several causes actually fired, which the
    old single-code collapse erased.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE

    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    # Read through ``getattr`` with a default because this function accepts
    # DUCK-TYPED conductors, not only the real class — the same reason
    # ``hasattr(snap, "attempt_history")`` reads defensively a few lines down.
    # Measured, not assumed: a direct attribute read fails 7 tests in
    # ``test_correction_crossover_v2_endpoints.py``, all of which persist a
    # stand-in that implements the fields their own assertion is about and
    # nothing else. An absent property is "nothing reserved", which is what the
    # key's own absence already means downstream.
    ripple_reservation = getattr(conductor, "measure_ripple_reservation", None)
    # Same duck-typed read, same reason as the line above: a stand-in conductor
    # predating the alignment-confidence demotion means "nothing reserved".
    alignment_reservation = getattr(
        conductor, "measure_alignment_reservation", None
    )
    # Same duck-typed read, same reason: a stand-in conductor predating audit
    # gauntlet 5a means "nothing reserved" here too.
    calibration_reservation = getattr(
        conductor, "measure_calibration_reservation", None
    )
    # #2923: same duck-typed read, on ``snap`` rather than ``conductor`` since
    # this one lives on ``V2ConductorSnapshot`` itself — a snapshot stand-in
    # built before this field existed means "not banked", which is what the
    # key's own absence already means downstream.
    measure_sweep_durations_s = getattr(snap, "measure_sweep_durations_s", None)
    # Same duck-typed read, same reason: a stand-in conductor that predates
    # this field is "no pilot evidence", which is what the key's own absence
    # means downstream. See the ``failure`` block below for what it renders.
    #
    # GATED ON THE CODE BEING PERSISTED, not on the conductor's own. The
    # caller supplies ``failure_code``, and several terminal arms supply one
    # the capture loop never produced — the relay-death arm persists
    # ``relay_timeout`` over whatever the last capture failed on. Ungated,
    # ``_persist_terminal_failure(c, "relay_timeout")`` after a heard-speaker
    # ``locate_failed`` writes ``{"code": "relay_timeout", "pilot_heard":
    # True}``: one failure's code carrying another's evidence, which is the
    # exact mispairing ``_pilot_heard_for`` refuses on the conductor side.
    # No terminal arm renders a wrong sentence from that pair today (only
    # ``locate_failed`` reads the key), but the drift surface is introduced
    # here, so the check belongs here too.
    failure_pilot_heard = (
        getattr(conductor, "last_failure_pilot_heard", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    # #2291: which arm of ``correction_rollback_failed`` this is. Gated on the
    # SAME code-agreement check for the same reason — a terminal arm passing a
    # different ``failure_code`` must not inherit this round's anchor fact and
    # render a sentence about a restore that code never attempted.
    failure_rollback_anchor = (
        getattr(conductor, "last_failure_rollback_anchor", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    # #2616: let the journey learn about a restore it could not see.
    #
    # ``observe_restore`` clears the durable ``applied`` in place and holds no
    # conductor, so a LIVE session that rolled back — the delta probe's own
    # ``rollback`` seam, or the round's adoption restore — kept ``applied``
    # True in memory. The write below reads that stale True off the snapshot
    # and put it straight back over the clear, which is one fact with two
    # owners and the durable one losing.
    #
    # Resolved in the owner's favour rather than by special-casing the write:
    # the durable state is the authority on whether a restore HAPPENED, the
    # journey is the owner of the flag, so this tells the journey and then
    # writes what it says. Scoped to the SAME session, because a prior
    # session's restore says nothing about this one.
    #
    # This is not the SF1 carry-forward's inverse and does not weaken it. That
    # guard (below) only ever sets True, protecting a stop that lands while an
    # apply is in flight; it reads ``prior`` too, so after this correction it
    # sees ``applied`` already False and correctly declines to fire.
    if (
        prior.get("applied") is False
        and prior.get("session_id") == snap.session_id
        and snap.applied
    ):
        conductor.note_restore_observed()
        snap = conductor.snapshot()
    if hasattr(snap, "attempt_history"):
        attempts_loop_state: dict[str, Any] | None = {
            "history": [
                item.to_dict()
                for item in (getattr(snap, "attempt_history", ()) or ())
            ],
            "last_decision": (
                dict(getattr(snap, "last_attempt_decision"))
                if getattr(snap, "last_attempt_decision", None) is not None
                else None
            ),
        }
    else:
        prior_attempts = prior.get("attempts_loop")
        attempts_loop_state = (
            dict(prior_attempts) if isinstance(prior_attempts, Mapping) else None
        )
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        # The phases THIS session runs — read by ``_phase_from_state`` so a
        # verify-only re-arm reaches "done" instead of waiting forever on a
        # position group it never had.
        "session_phases": list(snap.session_phases),
        # WHICH INSTRUMENT produced this state (flow-simplification §1.2).
        # Empty string means unknown — state written before tiers existed, or
        # a session that never declared one — and every reader must render it
        # as unknown rather than assuming "full": express makes no
        # cross-position post-apply claim at all, so guessing would attach a
        # claim the measurement never made (the same unknown-vs-default rule
        # ``echo_band_provenance`` carries, issue #1763).
        "tier": snap.tier,
        # WHERE the pre-apply cloud's close has got to (two-stage D1). The
        # wizard renders from this file alone, and "every stage-1 phase
        # accepted, no candidate" is true at three different moments — the
        # household holding a phone at the confirm screen, the fit running,
        # and a session that ended having produced nothing. Without this they
        # rendered as the third one, which offered to throw away a
        # measurement that was still in progress.
        "cloud_close": snap.cloud_close,
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        # #2923: MEASURE's realized per-role sweep duration, possibly fitted
        # to a declared limit (#2921) — banked so an offline rebuild
        # (``harmonic_evidence.rebuild_measure_program``) can replay a fitted
        # round's sweep instead of refusing PROGRAM_NOT_REPRODUCIBLE. ``None``
        # carries forward exactly like an absent ``gain_plan_db`` above: a
        # round banked before this field existed, or before MEASURE composed.
        "measure_sweep_durations_s": (
            dict(measure_sweep_durations_s) if measure_sweep_durations_s else None
        ),
        # S3 journey state. The conductor is the sole lifecycle owner and the
        # web host serializes its snapshot verbatim; `/state` below projects
        # only the last decision, never the full history. The store count is
        # read fresh from its persistence owner at the `/state` boundary.
        "attempts_loop": attempts_loop_state,
        "candidate": _candidate_summary(
            conductor.candidate,
            # ``getattr`` for ``_predicted_spec_prior``'s reason: this snapshot
            # serializes duck-typed conductors too, and a stand-in without the
            # property means "not pinned", which is what an ordinary round is.
            topology_pinned=(
                getattr(conductor, "topology_prescription_record", None) is not None
            ),
        ),
        "sound_design_revision": (
            getattr(conductor, "sound_design_revision", None)
            if getattr(conductor, "sound_design_revision", None) is not None
            else prior.get("sound_design_revision")
        ),
        # What MEASURE accepted WITH A RESERVATION (owner ruling 2026-08-03,
        # issue #2087). Absent/`None` means the accepted capture had nothing to
        # reserve about — never "we did not check", because this key is written
        # on every persist by a session that runs MEASURE.
        #
        # Its own block rather than a key on ``candidate`` above: that summary
        # projects the candidate ARTIFACT's own fields, and a reservation is a
        # verdict-time judgement about the capture the artifact was built from.
        # Folding it in there would make one dict have two owners.
        "measure": (
            {
                **(
                    {"ripple_reservation": dict(ripple_reservation)}
                    if ripple_reservation
                    else {}
                ),
                **(
                    {"alignment_reservation": dict(alignment_reservation)}
                    if alignment_reservation
                    else {}
                ),
                **(
                    {"calibration_reservation": True}
                    if calibration_reservation
                    else {}
                ),
            }
            if ripple_reservation or alignment_reservation or calibration_reservation
            else None
        ),
        "verify": (
            {
                "outcome": verify_outcome,
                # WHICH VERDICT produced that outcome (issue #1974). "inconclusive"
                # is reached by two verdicts sharing no mechanism — a VERIFY gate
                # shorter than MEASURE's, and the recording chain moving between
                # attempts — and the done screen must name the right one long
                # after the terminal failure screen has aged out of the render
                # path. It is NOT read from ``failure.code`` below: that is the
                # most recent rejection of ANY phase, and a later persist with
                # ``failure_code=None`` nulls it while this outcome still stands,
                # so the two are only coincidentally equal. Written by the
                # conductor with the outcome in one call (``_set_verify_outcome``),
                # so the pair cannot disagree.
                **(
                    {"code": conductor.verify_code}
                    if conductor.verify_code
                    else {}
                ),
                # WHAT THE GATE DID, on EVERY outcome (issues #1974 / #1966).
                # Same shape and same argument as the frame and the graded band
                # below: the sentence is
                # ``gate_disclosure.describe_gate``'s, composed once at verdict
                # time and rendered verbatim — a record that prints a 7 ms
                # window and nothing else reads as "reflections removed" to
                # every consumer, and across the 2026-07-30 corpus it meant "no
                # reflection found; window capped". ``reflection_measured``
                # beside it is the one fact the household copy branches on.
                **(
                    {"gate": dict(conductor.verify_gate)}
                    if conductor.verify_gate
                    else {}
                ),
                # The verify_fail expert-disclosure numbers (#1605) — persisted
                # only for a NON-pass outcome (the only one that renders a
                # verify_fail screen). A pass shows the candidate_review card,
                # not these tracking numbers, so it keeps its lean shape.
                **(
                    {"evidence": dict(conductor.verify_evidence)}
                    if (verify_outcome != "pass" and conductor.verify_evidence)
                    else {}
                ),
                # WHAT SPAN was graded, on EVERY outcome including a pass
                # (#1868). This rode ``evidence`` above until now, i.e. it
                # reached a surface only once the verdict had already failed —
                # so the one screen that says "Verified." was the one screen
                # that never said over what. The band is not the nominal
                # Fc±1 octave: two clamps move its lower edge up, and on the
                # 2026-07-30 corpus it sat at [2000, 4000] Hz while the
                # crossover defect under investigation sat at 1919 Hz. Same
                # shape as ``delta_probe`` below, and for the same reason.
                **(
                    {"graded_band_hz": list(conductor.verify_graded_band_hz)}
                    if conductor.verify_graded_band_hz
                    else {}
                ),
                # WHAT FRAME the comparison spanned, on EVERY outcome (rung
                # P1). Same shape and same argument as the graded band above:
                # VERIFY differences an on-axis MODEL against an in-room
                # MEASUREMENT, and on the 2026-07-29 corpus a single
                # −0.79 dB/oct tilt between those two frames was 84% of the
                # flow's apparent prediction error. The raw numbers are
                # untouched — this says how much of them was the instrument,
                # and a pass is exactly when nobody would otherwise ask.
                **(
                    {"frame": dict(conductor.verify_frame)}
                    if conductor.verify_frame
                    else {}
                ),
                # WHICH OF §7's CLAIMS WERE PROVED, on EVERY outcome including
                # a pass (R18, #1868). Same shape and argument as the graded
                # band and frame above, one step further: those bound how wide
                # and how honest a claim is, this says which claims exist. Two
                # of the four are structurally not-evaluated (VERIFY plays one
                # summed sweep), and "Verified." over an unstated claim set
                # reads as all four.
                **(
                    {"claims": dict(conductor.verify_claims)}
                    if conductor.verify_claims
                    else {}
                ),
                # The level-reference reset this session performed, when the
                # previous session's reference differed enough to be worth
                # saying (#1927). Same every-outcome shape as the graded band
                # above: a pass is exactly when an unstated reset would let a
                # household read cross-day identity into a same-session claim.
                # Absent means there was nothing to disclose, never "we did not
                # reset" — the reset is now unconditional.
                **(
                    {"level_reference": dict(conductor.verify_level_reference_reset)}
                    if conductor.verify_level_reference_reset
                    else {}
                ),
                # (A "flatness" key lived here until the flat-linearization
                # plan's PR-5, carrying the retired per-VERIFY-capture
                # construction. It is gone rather than repointed: the spec
                # verdict is a property of the CLOUD, so it belongs in the
                # "cloud" block below and nowhere else — a second copy under
                # "verify" is exactly the duplicated-frame shape PR-5 exists
                # to remove. A state file written by an older build may still
                # carry the stale key; nothing reads it.)
                #
                # The delta probe's verdict, on EVERY outcome including a pass
                # (#1811). A non-rollback non-matched verdict — ``level_mismatch``
                # and ``frame_mismatch`` — otherwise reached no surface at all: the
                # refusal path ignores it by design, so the household saw a
                # clean "Verified." over a shape question the probe never got
                # to answer. A summary, not the whole map: the verdict, why, and
                # the numbers each non-rollback verdict is a claim ABOUT — see
                # ``_delta_probe_summary``. The full record stays in the journal.
                **(
                    {"delta_probe": _delta_probe_summary(conductor.delta_probe)}
                    if getattr(conductor, "delta_probe", None) is not None
                    else {}
                ),
            }
            if verify_outcome is not None else None
        ),
        "failure": (
            {
                "code": failure_code,
                # WHEN this failure happened (issue #1942). Without it the
                # envelope could not tell a failure the household is looking
                # at right now from one a previous day's session left behind,
                # so it re-rendered the terminal screen — with that session's
                # verify numbers — on every later page load, forever.
                #
                # The file-level ``updated_at`` cannot answer this: it is
                # last-write-of-ANYTHING, so it moves for reasons that have
                # nothing to do with the failure. This is the failure's own
                # clock, and it belongs on the failure's own record.
                #
                # Epoch float, deliberately the same type and clock as
                # ``save_v2_state``'s ``updated_at`` one level up — an age is
                # then a subtraction, with no format to parse and no second
                # time representation in one file.
                #
                # Stamped at write, not carried forward, because every writer
                # is an in-session capture-loop event: a failing session may
                # persist the same code twice (the rejecting capture, then the
                # plan-finished write) seconds apart, and NO read path
                # persists — ``handle_status`` never writes — so nothing can
                # refresh this while a household stares at the screen.
                "at": time.time(),
                **(
                    {"refusals": [str(slug) for slug in failure_refusals]}
                    if failure_refusals else {}
                ),
                # WHAT THE CAPTURE MEASURED about the speaker being audible —
                # ``locate_failed``'s copy branches on it (#2085), so the
                # envelope cannot render this failure's honest sentence
                # without it. Unlike ``refusals`` above this is NOT forensics:
                # it reaches the household, through
                # ``crossover_envelope_v2._reason_message``.
                #
                # Present only when established. Absent and ``False`` mean
                # different things and both are already handled downstream —
                # absent is "no pilot evidence" (every failure that ran no
                # capture, plus every state file written before this shipped),
                # ``False`` is "the pilot was measured and did not clear the
                # room". Writing a bare ``False`` for the unknown case would
                # turn a missing measurement into a claim about the room.
                #
                **(
                    {"pilot_heard": bool(failure_pilot_heard)}
                    if failure_pilot_heard is not None else {}
                ),
                # #2291, on exactly the key above's terms: absent is "the
                # question does not apply to this code, or predates the
                # record", and the copy owner reads absent as the Undo arm.
                # Writing a bare False for the unknown case would tell a
                # household with a perfectly good anchor that they have none.
                **(
                    {"rollback_anchor_available": bool(failure_rollback_anchor)}
                    if failure_rollback_anchor is not None else {}
                ),
            }
            if failure_code else None
        ),
        # Position-group outcome (PR-3b): the closing geometry verdict and the
        # position ids behind it, per group. Present only for groups that have
        # CLOSED — an absent key means "still walking", never "geometry was
        # fine". PR-4 adds the rest of the honest-instrument output (exclusion
        # screen, null registry, spec curve) alongside this block; the geometry
        # verdict is what PR-3b measured and so what PR-3b persists.
        "cloud": _cloud_summary(conductor),
        # No ``fc_selection`` key: the corner selector that produced one is
        # retired (ticket 2.4) and the field is absent from this version of the
        # record rather than written as a null. Rounds banked while a selector
        # existed keep theirs in durable state; no product read path reads it
        # back (the offline archaeology scripts still do, deliberately).
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            # Two-stage commission D4: the spec verdict for the curve above,
            # graded ONCE by the conductor's accountability seam against the
            # full-resolution tuple — this is a copy of that one report, never
            # a re-grade of the decimation the line above just wrote.
            # ``None`` means ungradeable, which is not a pass.
            #
            # Threaded through exactly like ``gate_window_ms`` below, and
            # rehydrated by the verify-only re-arm for the same reason: a
            # verify-only re-arm builds a fresh conductor that never runs a
            # fit, so a verdict that did not travel that route would be
            # dropped on the first "Try again" — the ``cloud`` B1 bug shape,
            # one field over.
            "predicted_spec": _predicted_spec_prior(conductor),
            # The delta probe's COMMANDED axis (#2291 Phase 3). Produced by the
            # stage-1 fit and consumed by the stage-2 probe — which runs in a
            # different process, against a conductor that never ran a fit — so
            # exactly like ``predicted_sum`` above, this durable state is the
            # only channel it has. Until it crossed, every shipped stage 2
            # graded its correction with the shortfall-vs-model-error
            # discriminator switched off, reporting ``unavailable``.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing commanded" — which is what
            # the key's own absence already means downstream.
            "commanded_delta": _decimate_delta(
                getattr(conductor, "measure_commanded_delta", None)
            ),
            # The delta probe's STATE axis beside its CHANGE axis (#2614): what
            # the applied graph declares it does against the uncorrected
            # crossover. It crosses for exactly ``commanded_delta``'s reason and
            # is read the same way — a stand-in without the property means "no
            # state axis", which downstream degrades to the change axis alone
            # for the two directional safety rules.
            "declared_transfer": _decimate_delta(
                getattr(conductor, "measure_declared_transfer", None)
            ),
            # The MEASURED side of the same comparison (#2522). Its two
            # neighbours above are what the correction PREDICTED and COMMANDED;
            # without this one, a disputed probe verdict could only be
            # re-examined by measuring the speaker again, because the evidence
            # that produced it lived in one process's memory and died with it.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing measured" — which is what the
            # key's own absence already means downstream.
            "verify_measured": _decimate_verify_measured(
                getattr(conductor, "verify_tracking_curve", None)
            ),
            # #2662's provenance. It crosses for exactly ``commanded_delta``'s
            # reason — produced by the stage that MEASURES the candidate, banked by
            # the stage that GRADES it, and those are different sessions in
            # different processes — and is read the same way, through
            # ``getattr``, so a duck-typed stand-in without the property means
            # "no prescription", which is what the key's own absence already
            # means downstream.
            "alignment_prescription": getattr(
                conductor, "alignment_prescription_record", None
            ),
            # The crossover pin, crossing on the identical route and read the
            # same way. It carries MORE weight than its neighbour, not less:
            # stage 2 re-opens at the topology this names, so without it the
            # VERIFY of a 4000 Hz round would be graded against the incumbent
            # corner's design target — the applied graph judged for not being
            # the crossover it replaced.
            "topology_prescription": getattr(
                conductor, "topology_prescription_record", None
            ),
            # A9's provenance, and it crosses for the line above's reason, read
            # the same way: the stage that TAKES a blend prescription is stage 1
            # and the stage that banks the round's receipt is stage 2, so
            # durable state is the only channel it has. ``None`` means the
            # round's blend correction came from decision 10's solver, which is
            # what every automatic round banks — so a series read back later can
            # attribute an outcome to the class that produced it, which is the
            # comparison the prescriber loop exists to make possible.
            "blend_prescription": getattr(
                conductor, "blend_prescription_record", None
            ),
            # …and WHICH document asked, the fact that lets a reader six weeks
            # later find the evidence packet and the conversation behind the
            # numbers. Its own key rather than a field inside the record above,
            # on ``alignment_objective``'s rule — and here that rule is
            # load-bearing rather than tidy: the record has to round-trip
            # through ``blend_prescription_from_mapping`` for stage 2 to
            # rehydrate it, and that reader refuses an unknown field instead of
            # ignoring it, so a digest nested inside would make the whole record
            # unreadable.
            "blend_prescription_sha256": str(
                getattr(conductor, "blend_prescription_sha256", "") or ""
            ),
            # …and WHICH commitment the fit reached, the fact that turns the
            # block above from "this candidate was asked for" into "this
            # candidate ran".
            # Its own key rather than a field inside the prescription: the
            # prescription is the REQUEST and this is the OUTCOME, they are
            # written at different moments by different owners, and nesting one
            # in the other would make a round that prescribed nothing have
            # nowhere to record an objective it still has.
            "alignment_objective": getattr(
                conductor, "measure_alignment_objective", "",
            ),
            # #2291's measured "before": the summed capture stage 1 takes at
            # the mark immediately before apply, which stage 2's benefit
            # verdict differences its own capture against. Produced in one
            # process and graded in another, so — exactly like
            # ``predicted_sum`` and ``commanded_delta`` above — this durable
            # state is the only channel it has.
            #
            # Already bounded: the record's curve is reduced to
            # ``round_evidence.BENEFIT_CURVE_MAX_BINS`` (the same 512
            # ``_decimate_sum`` applies) at capture time, on BOTH sides of the
            # comparison, so no decimation belongs here — re-gridding one side
            # after the fact is how a grid mismatch gets manufactured.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason,
            # and ``to_dict`` behind a type check for the same one: a duck-typed
            # conductor without the property, or with something that is not an
            # ``EntryBaseline``, means "no baseline" — which is what the key's
            # own absence already means downstream — never a raise that loses
            # the whole snapshot.
            "entry_baseline": _entry_baseline_prior(conductor),
            # What stage 1 PROPOSED, as an identity (#2392). The round receipt
            # is written by the stage that GRADES, which runs in a different
            # process against a conductor that never planned anything — so,
            # exactly like ``commanded_delta`` and ``entry_baseline`` above,
            # this durable state is the only channel the fingerprint has.
            #
            # The fingerprint travels, never the proposal: reassembling one at
            # VERIFY out of the decimated priors around it would digest to a
            # different value, and a receipt naming a proposal that never
            # existed is worse than one naming the candidate honestly.
            #
            # Read through ``getattr`` for ``_predicted_spec_prior``'s reason:
            # this function persists duck-typed conductors too, and a stand-in
            # without the property means "nothing proposed" — which is what the
            # empty string already means downstream.
            "proposal_fingerprint": str(
                getattr(conductor, "measure_proposal_fingerprint", "") or ""
            ),
            "gate_window_ms": conductor.measure_gate_window_ms,
            # Measurement-honesty gate G3's reference, DATED — history, not a
            # comparator (#1927). The verify-only re-arm hands it to the next
            # conductor as ``verify_pilot_transfer_prior``, which may only
            # disclose it; the constructor argument that used to make it that
            # session's live baseline is gone. Carried forward below when this
            # session set no reference of its own, so a re-arm that dies before
            # its first usable VERIFY attempt does not erase the history.
            #
            # The retired flat ``pilot_transfer_baseline`` key is deliberately
            # NOT read anywhere any more: it carried no date, so it can be
            # neither compared against (the ruling) nor shown as history
            # (#1942). No migration is needed — this whole ``verify_priors``
            # dict is rebuilt on every persist, so an older build's key is
            # inert until the first write of the next session drops it.
            "pilot_transfer_reference": conductor.verify_pilot_transfer_reference,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    # A conductor that declares no tier of its own — the verify-only re-arm,
    # which re-runs one tracking capture against an ALREADY-applied result —
    # must not erase which instrument produced that
    # result. Carried forward unconditionally, exactly like ``cloud`` below
    # and for the same reason: the re-arm runs under a brand-new relay session
    # id, so a session-scoped guard would drop it on the first "Try again".
    if not state["tier"] and prior.get("tier"):
        state["tier"] = str(prior["tier"])
    # G3's dated reference (#1927) carries forward across the writes of a
    # VERIFY-ONLY session, and is dropped by any session that MEASURES.
    #
    # Carried, because every capture of a verify session persists and the
    # first writes run BEFORE any usable VERIFY attempt has set this session's
    # own reference: without this the opening write of a re-arm would blank
    # the history the disclosure reads, and a session that died before its
    # first tone would erase it for good. Same shape as ``tier`` above — the
    # re-arm runs under a brand-new relay session id, so a session-id guard is
    # the wrong one (the ``cloud`` B1 bug, one field over).
    #
    # Dropped by a measuring session, because a pilot transfer is captured
    # THROUGH the applied graph: once a new candidate is applied the two
    # numbers answer different questions, and a disclosure computed across
    # that boundary would report a graph change as a level-reference move.
    #
    # State the predicate honestly: this tests "does THIS SESSION'S PLAN
    # contain MEASURE", which is COARSER than "the graph changed". A session
    # that measures and never applies — a refused candidate, an abandoned
    # walk — drops the history even though nothing moved. That is the
    # fail-silent direction and the one to be coarse in: the cost is a
    # disclosure that goes unsaid, against a disclosure that says something
    # untrue. Binding this to the applied candidate's fingerprint instead
    # would be exact, and is deliberately not built for a report-only line.
    if PHASE_MEASURE in snap.session_phases:
        state["verify_priors"]["pilot_transfer_reference"] = None
    elif state["verify_priors"]["pilot_transfer_reference"] is None:
        prior_reference = (prior.get("verify_priors") or {}).get(
            "pilot_transfer_reference"
        )
        if isinstance(prior_reference, Mapping):
            state["verify_priors"]["pilot_transfer_reference"] = dict(prior_reference)
    # #2291's entry baseline needs NO carry-forward, and that is worth stating
    # because its immediate neighbour above needs one. The difference is where
    # the fact lives. ``pilot_transfer_reference`` is seeded into the conductor
    # as history it may only DISCLOSE (#1927) and is not what
    # ``verify_pilot_transfer_reference`` reports, so a re-arm's own persist
    # really would blank it. The entry baseline is seeded into the SAME field
    # its own capture writes (``measure_entry_baseline``) — exactly like
    # ``predicted_sum`` and ``commanded_delta``, neither of which carries
    # forward either — so a stage-2 persist re-writes the record its conductor
    # was constructed with.
    #
    # Mutation-verified rather than argued (2026-08-11): a carry-forward branch
    # was written here first, and deleting it changed no test outcome, because
    # no path reaches it. Keeping it would have been a branch nothing can
    # distinguish, and it would have weakened the real pin — that a MEASURING
    # session replaces the previous round's "before" instead of letting this
    # round's "after" be differenced against a stale one, which is exactly the
    # false comparison #2291 exists to stop.
    #
    # The applied flag is host-durable (set by the apply endpoint) — never
    # regressed by a conductor snapshot that predates it.
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        # A verify-only re-arm mints a new relay session around the already-
        # applied candidate. Its conductor intentionally has no candidate
        # object of its own, but the fingerprint is the attempts loop's stable
        # write identity; erasing it here turns recovery into a second record.
        # A measuring session still keeps the old session-scoped rule so a new
        # journey cannot inherit a stale candidate before it builds its own.
        if (
            prior.get("session_id") == snap.session_id
            or (
                prior.get("applied") is True
                and PHASE_MEASURE not in snap.session_phases
            )
        ):
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])
    # B1 fix (flat-linearization plan PR-4 review, 2026-07-26): ``cloud``
    # carries the SAME session-id-gated shape as ``candidate``/``evidence``
    # above, which is the WRONG guard for it — a verify-only re-arm's
    # conductor (the re-arm's ``index_phase_map={1: PHASE_VERIFY}``)
    # has NO group phase in ITS OWN session, so ``_cloud_summary`` always
    # returns ``None`` for it: not because nothing closed, but because there
    # is nothing to close in this session. A session-id gate would never
    # carry the prior cloud verdict forward — exactly the same shape of bug
    # ``pre_apply_profile``'s own comment below documents ("the deferred
    # VERIFY that auto-arms right after every apply runs under a BRAND-NEW
    # relay session id"), and it hit on the very first tap of "Try again"
    # (the PRIMARY next_action after a failed verify): the cloud verdict a
    # household had just walked a session for went blank on `/state`, the
    # envelope, and the doctor's read, all three at once.
    #
    # Carry ``cloud`` forward UNCONDITIONALLY whenever THIS conductor's own
    # session has no group phase to report on — mirroring
    # ``pre_apply_profile``'s unconditional carry-forward below, not
    # ``candidate``/``evidence``'s session-scoped one above, which is the
    # wrong shape for this path. A conductor that DOES have a group phase in
    # its own session is left alone: ``_cloud_summary``'s own ``None`` there
    # honestly means "this session's group has not closed yet" and must not
    # be papered over with a stale prior verdict.
    from jasper.active_speaker.crossover_v2.journey import GROUP_PHASES, PHASE_MEASURE

    conductor_session_phases = set(getattr(conductor, "session_phases", ()) or ())
    if not (conductor_session_phases & GROUP_PHASES):
        if state["cloud"] is None and isinstance(prior.get("cloud"), Mapping):
            state["cloud"] = dict(prior["cloud"])
        # The cloud bundle-artifact fingerprints ride inside `evidence`
        # (`refs["cloud_artifacts"]`) — a group-phase-less session's own
        # `publish_cloud` seam is never wired (nothing to publish), so its
        # own `evidence` dict never carries this key forward on its own.
        # Restore it from prior rather than losing it to whatever this
        # session's own evidence looks like.
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and "cloud_artifacts" in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                "cloud_artifacts", prior_evidence["cloud_artifacts"]
            )
            state["evidence"] = merged_evidence
    # The household-readable findings projection (CC1), carried forward on its
    # OWN predicate rather than the group-phase one above. A finding is banked
    # by the fit, which runs in MEASURE; a session that does not run MEASURE
    # never had the chance to produce one, so its empty projection is a
    # timestamp rather than a verdict — the same distinction ``cloud``'s carry
    # forward draws one block up, keyed on the phase that actually produces the
    # value. This is what puts the measuring session's finding on the DONE
    # screen: stage 2 is a different session in a different bundle
    # (the verify-only re-arm opens a new one), and without this the result
    # screen would silently lose what the measurement learned.
    #
    # The converse matters just as much: a session that DOES run MEASURE writes
    # its own projection, empty included, so a fresh measurement that banks
    # nothing clears a previous session's finding instead of replaying it.
    if PHASE_MEASURE not in conductor_session_phases:
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and FINDING_HOUSEHOLD_REFS_KEY in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                FINDING_HOUSEHOLD_REFS_KEY,
                prior_evidence[FINDING_HOUSEHOLD_REFS_KEY],
            )
            state["evidence"] = merged_evidence
        # G1's reservation (#2087) carries forward on the SAME predicate and
        # for the same reason as the findings projection above: it is banked
        # by MEASURE, and the DONE screen is rendered by stage 2 — a different
        # session, in a different bundle, whose conductor never ran MEASURE and
        # so would persist ``None`` over it. Without this line the household
        # would be shown the reservation on the screen where they DECIDE and
        # then not on the screen that tells them the speaker is tuned, which is
        # the worse half to lose.
        #
        # The converse holds too, again like findings: a session that DOES run
        # MEASURE writes its own value, ``None`` included, so a fresh clean
        # measurement clears a previous session's reservation rather than
        # replaying a caveat about a capture that has been superseded.
        if isinstance(prior.get("measure"), Mapping) and state["measure"] is None:
            state["measure"] = dict(prior["measure"])
        # The Fc selection carried across the same seam for the same reason
        # while a corner selector existed. It is retired (ticket 2.4), no
        # session writes the field, and no product read path reads it back — so
        # there is
        # nothing to carry, and a legacy value ages out of durable state on the
        # next write rather than being copied forward into a record whose
        # version has no such field.
    # ``pre_apply_profile`` (the Undo stash — observe_apply_success /
    # handle_v2_restore), ``expected_post_apply_offset_db`` (the apply's own
    # declared level move — observe_apply_success / the delta probe's
    # ``applied_offset_db`` seam), and ``apply_blocked`` (the
    # auto-apply-failed nudge, layered onto the "applying"-phase
    # fix_and_retry screen — owner ruling, 2026-07-20) are NOT conductor-owned
    # fields: the conductor neither produces nor reads any of them, so they
    # are absent from the ``state`` literal above and every OTHER caller of
    # this function only ever sets the fields it does know about.
    #
    # THREE separate P0s have now been caused by a host-owned key being added
    # to ``observe_apply_success`` without a line here (pre_apply_profile
    # W6.12; cloud B1; this offset, #1811).
    # ``test_every_host_owned_apply_key_survives_persist_conductor_state``
    # derives the host-owned set mechanically — whatever the apply path gives a
    # value that a conductor-only persist cannot regenerate — and fails on the
    # FOURTH. The fix is a carry-forward line here. Only a key that genuinely
    # wants session scoping (like ``apply_blocked``) belongs in that test's
    # exception set instead, and it has to say why.
    #
    # They carry forward with OPPOSITE session-scoping, by design:
    #
    #   * ``pre_apply_profile`` is carried forward UNCONDITIONALLY (not gated
    #     on a matching session_id): the deferred VERIFY that auto-arms right
    #     after every SUCCESSFUL apply runs under a BRAND-NEW relay session id
    #     (the verify-only re-arm mints one and "rebinds" the conductor's
    #     session_id before its own ``persist_conductor_state`` call), so a
    #     session-id-gated carry-forward would lose the stash on that very
    #     first post-apply snapshot. W6.12 P0: without this, the verify phase
    #     that always immediately follows an apply wiped the just-stashed
    #     ``pre_apply_profile`` before a household could ever reach the
    #     verify_fail Undo screen — ``/crossover/v2/restore`` 400'd with "no
    #     previous crossover to restore to" after literally every apply.
    #
    #   * ``apply_blocked`` IS session-scoped (#1605): it is only ever set on
    #     a BLOCKED auto-apply, which — unlike a successful one — refuses the
    #     deferred VERIFY outright (the honest ``apply_failed`` reason, never a
    #     re-arm), so it never has to survive the verify-only re-arm's
    #     new-session rebind. Gating it drops a stale nudge the moment a fresh
    #     session begins instead of leaking session A's blocker onto session
    #     B's apply step.
    state["pre_apply_profile"] = prior.get("pre_apply_profile")
    # ``expected_post_apply_offset_db`` (#1811) is the THIRD field in this
    # host-owned class and takes ``pre_apply_profile``'s unconditional shape
    # for the identical reason: ``observe_apply_success`` writes it, the
    # conductor neither produces nor reads it, so it is absent from the state
    # literal above and every call to this function would otherwise erase it.
    #
    # It was erased, on every call, until this line existed — and the two
    # readers that lost it are exactly the two the post-apply flow depends on.
    # The CLOUD_VERIFY probe (the one that carries the spatial arm AND rollback
    # authority) re-classifies after the group closes, by which time several
    # captures have persisted: it would have graded the apply's own headroom
    # charge blind and could roll a healthy correction back — the precise
    # failure this key exists to prevent, one phase later. And
    # the verify-only re-arm persists under a brand-new session id, so
    # every "Try again" probe would have been blind too.
    #
    # Session-scoping it would reintroduce that second half: like
    # ``pre_apply_profile``, this must survive the new-session rebind.
    state["expected_post_apply_offset_db"] = prior.get(
        "expected_post_apply_offset_db"
    )
    state["accepted_sound_revision"] = (prior.get("accepted_sound_revision")
        if PHASE_MEASURE not in snap.session_phases else None)
    # Takes ``accepted_sound_revision``'s session-gated shape, not
    # ``sound_declaration_undo``'s unconditional one, because it is scoped to
    # exactly that token: it is the inverse of a save the apply has not yet
    # committed to a graph, readable only while the review that saved Sound is
    # still the current one. A fresh MEASURE clears the token, and a record
    # that outlived it would be an inverse nothing can apply.
    state["accepted_sound_declaration_change"] = (
        prior.get("accepted_sound_declaration_change")
        if PHASE_MEASURE not in snap.session_phases else None)
    # #2292's declaration-undo record is the FOURTH key in the host-owned class
    # described above — ``observe_apply_success`` writes it beside
    # ``pre_apply_profile``, the conductor neither produces nor reads it — so it
    # takes that key's UNCONDITIONAL shape, and the mechanical guard
    # ``test_every_host_owned_apply_key_survives_persist_conductor_state``
    # covers it automatically.
    #
    # Deliberately NOT the shape of ``accepted_sound_revision`` directly above,
    # even though the same event makes both non-null — an apply whose candidate
    # crosses somewhere other than the declaration, today an operator's topology
    # pin (``declaration_change_for_candidate`` is the live gate; the retired
    # alternative-Fc accept was the original one):
    #
    #   * ``accepted_sound_revision`` is the REVIEW-binding token
    #     ``_update_current_review`` gates the apply on. It is scoped to the
    #     review that saved Sound, so a fresh MEASURE must clear it or the next
    #     accept would skip its own Sound save.
    #   * this record is the inverse of a LIVE applied declaration, which
    #     outlives that review exactly as long as the graph does. Session-
    #     scoping it would reproduce the W6.12 P0 one field over: the deferred
    #     VERIFY that auto-arms after every apply persists under a brand-new
    #     session id, so the very first Undo after every apply would find no
    #     record and leave ``/sound`` declaring the undone crossover.
    state["sound_declaration_undo"] = prior.get("sound_declaration_undo")
    # #2537's round anchor is the FIFTH key in the same host-owned class, and
    # takes the same unconditional shape for the same reason — ``round_anchor``
    # describes the live apply, and the deferred VERIFY that auto-arms after
    # every apply persists under a brand-new session id, so session-scoping it
    # would blank the restore's divergence check on the very first re-arm.
    state[ROUND_ANCHOR_STATE_KEY] = prior.get(ROUND_ANCHOR_STATE_KEY)
    state["apply_blocked"] = (
        prior.get("apply_blocked")
        if prior.get("session_id") == snap.session_id
        else None
    )
    # #2291: WHERE this round's receipt landed — round id plus the bundle
    # artifact's fingerprint, so the next round resolves the previous one by
    # identity instead of scanning bundles. Carried forward like the anchor
    # keys above rather than session-scoped: the receipt describes the graph
    # currently on the speaker, and that outlives the session that wrote it.
    # A conductor that graded no round contributes ``None``, which must not
    # erase the identity a previous one recorded.
    receipt_identity = _round_receipt_identity(conductor)
    state["round_receipt"] = (
        receipt_identity
        if receipt_identity is not None
        else prior.get("round_receipt")
    )
    # How many times the ordinal sequence has been RESET, carried forward
    # unconditionally like the anchor keys above and for a sharper version of
    # their reason: the conductor cannot contribute this — only the two reset
    # doors increment it — and it has to outlive the very session those doors
    # create. Session-scoping it would erase the disclosure on the first persist
    # after a reset, which is exactly the round it exists to label.
    #
    # Imported here rather than at module scope: ``coordinator`` pulls
    # ``program_analysis`` and the numpy stack, and this module is on the
    # socket-activated web host's import path — the package ``__init__``'s own
    # rule. The two web-side callers of the same pair import it function-local
    # for the same reason.
    from .coordinator import (
        ROUND_ORDINAL_EPOCH_STATE_KEY,
        round_ordinal_epoch_from_state,
    )

    state[ROUND_ORDINAL_EPOCH_STATE_KEY] = round_ordinal_epoch_from_state(prior)
    return ConductorState(state, receipt_identity is not None)
