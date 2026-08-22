# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One round's banked evidence, gathered into one document a reader can answer.

The crossover domain's sibling of
:func:`jasper.correction.evidence.build_evidence_packet`, which does exactly
this job for room correction and has done since the calibration advisor
shipped.  Same shape, same posture, same privacy vocabulary; different domain,
different artifacts, and deliberately no import in either direction — the two
domains bank to different roots in different formats, and a shared
implementation would have to be told which one it was reading on every field.

**Why it exists.**  A round already banks everything a reader needs: the
receipt's verdicts and axes, the cloud's curve and per-position evidence, the
spec's per-band honesty, the capture integrity per position, the identity of
the speaker and the microphone.  What it does not have is ONE document.  Every
session that wanted to reason about a round has re-walked that tree by hand —
most recently 635 lines of throwaway glue in a captures directory, which
recovered the mic angle by regex over a log file and hardcoded a measured
delay as a literal.  This module is the promotion of the *reading* half of
that glue, and nothing else: it computes no new number, grades nothing, and
writes nothing.

**Its one impurity, named.**  It reads JSON files under a directory.  That is
the same bounded impurity :mod:`.round_anchor` declares, and it is the whole
of it: no clock, no network, no CamillaDSP handle, no session.

**The packet's first duty is to say what is NOT in it.**  Copying the honest
fields verbatim is necessary and not sufficient — a reader also has to know
which questions this round cannot answer at all.  Three examples this survey
actually found on the shipped corpus, all carried as
``not_evaluated`` entries rather than omitted:

* **the microphone's angle at each position** is nowhere in the banked tree;
  only a coarse ``role`` (``onax``/``offax``) is.  The lab recovered angles by
  reading a walk-driver log.  A packet that quietly emitted ``role`` alone
  would let a reader assume the angle was simply not interesting.
* **per-branch verify claims** come back ``not_evaluated`` with the reason
  ``no_per_branch_verify_capture``.  That string is copied through untouched.
  Flattening it to a ``null`` — or worse, to a zero — would turn "we did not
  look" into "we looked and found nothing".
* **harmonic distortion** is computable but never banked, so no round in the
  corpus carries one.

**Absence has two flavours and they are never merged.**  ``source_absent``
means the artifact was not handed to this builder; ``field_null`` means it was,
and the field inside it is null.  The glue could not tell those apart (its
``or {}`` chains collapsed both), and they send a reader to different places —
"pass the state file too" versus "that stage did not run".

**Redaction is an allowlist, and it reports what it dropped.**  Same mechanism
as :func:`jasper.calibration_agent.advisor_context.build_advisor_context`:
copy named fields, and publish the names of the fields that were withheld so
the packet cannot quietly become a different document than the tree it came
from.  Absolute filesystem paths, raw WAV bytes, and household-authored prose
never enter.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)

from .alignment_prescription import alignment_prescription_response_format
from .blend_prescription import prescription_response_format
from .topology_prescription import topology_prescription_response_format
from .driver_prescription import (
    driver_passbands_from_safety_profile,
    driver_prescription_response_format,
)
from .feature_classification import (
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    FeatureVerdict,
    read_feature_verdicts,
)

__all__ = [
    "CLASSIFICATION_ARTIFACT",
    "NO_ROUND_ARTIFACTS_REASON",
    "PACKET_KIND",
    "PACKET_SCHEMA_VERSION",
    "CrossoverEvidencePacketError",
    "build_crossover_evidence_packet",
    "packet_driver_passbands_hz",
    "packet_feature_classifications",
    "packet_positional_evidence",
    "packet_region_band_hz",
    "round_artifact_dir",
    "round_program_dir",
]

#: Bumped when a reader that understood the previous version would misread this
#: one — never merely because the document grew. Added blocks, and a widened
#: block whose existing fields are untouched, leave every v1 field saying what
#: it said, so they stay at 1; that rule is pinned by
#: ``test_added_packet_blocks_do_not_bump_the_packet_schema_version``.
#:
#: It is the EVIDENCE document's version and nothing else. A prescription
#: answering this packet carries its own separate
#: :data:`~.blend_prescription.PRESCRIPTION_SCHEMA_VERSION`, and THAT is the
#: number :func:`~.blend_prescription.read_blend_prescription` refuses an
#: unknown value of.
PACKET_SCHEMA_VERSION = 1

PACKET_KIND = "jts_crossover_v2_evidence_packet"

GENERATED_BY = (
    "jasper.active_speaker.crossover_v2.evidence_packet."
    "build_crossover_evidence_packet"
)

#: Where a session bundle keeps its round artifacts. The ``<cap-id>`` directory
#: under it is the relay session id, which is NOT the bundle's own
#: ``session_id`` — the two namespaces are distinct on disk and conflating them
#: is how a reader ends up joining the wrong round to the wrong bundle.
_EVIDENCE_GLOB = "evidence/v1/artifacts/crossover_v2/*"

#: :func:`round_artifact_dir`'s reason when no
#: ``evidence/v1/artifacts/crossover_v2/<relay>/`` directory exists under the
#: given path at all. Named so a caller can distinguish this specific
#: refusal from "bundle carries more than one round" without parsing prose —
#: :mod:`jasper.cli.classify_features` appends shape guidance only here,
#: never on the two-round refusal, where "point at a different shape" is not
#: the fix.
NO_ROUND_ARTIFACTS_REASON = "no crossover_v2 round artifacts under evidence/v1"

#: Position fields copied verbatim. ``wav_path`` is deliberately absent — it is
#: an absolute path on the speaker's filesystem, and ``wav_sha256`` identifies
#: the same bytes without naming where they live.
_POSITION_FIELDS = (
    "position_id",
    "index",
    "attempt",
    "role",
    "take_id",
    "wav_sha256",
    "validity_floor_hz",
    "gate_disclosure",
    "gate_floor_source",
    "gate_window_ms",
    "gating_applied",
    "glitch_detected",
    "summed_ripple_db",
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
#:
#: :mod:`.feature_classifier` writes it — one name shared by the instrument
#: that produces a classification, the packet that reads it and the gate that
#: acts on it, so none of the three can invent its own spelling. No stage of a
#: round writes it AUTOMATICALLY: the instrument is an offline run over a
#: round's banked captures (``jasper-classify-features``), so the file is
#: present when somebody classified the round and absent otherwise. An
#: operator's own banked lab result carrying this name is read identically.
#: Its absence is an ordinary ``source_absent`` and is reported, never papered
#: over.
CLASSIFICATION_ARTIFACT = "feature_classification.json"

#: Verify-claim and state fields the packet carries. ``household_findings`` is
#: NOT among them and never will be: it is household-authored prose, so it is
#: both the one privacy-sensitive field in the tree and the only place a string
#: from outside JTS could reach a reader's instructions.
_STATE_WITHHELD = ("household_findings",)


class CrossoverEvidencePacketError(ValueError):
    """The named directory is not a crossover-v2 session bundle."""


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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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

    The allowlist mechanism from ``advisor_context._copy_allowed``, reproduced
    for this domain's records. Reporting the withheld NAMES is the part that
    matters: a packet that silently narrowed its source would be a different
    document wearing the same schema version.
    """
    if not isinstance(raw, dict):
        return {}, []
    kept = {key: raw[key] for key in allowed if key in raw}
    withheld = sorted(key for key in raw if key not in allowed)
    return kept, withheld


def round_artifact_dir(session_dir: Path) -> tuple[Path | None, str]:
    """The one round-artifact directory in a bundle, or ``(None, why)``.

    Public because a bundle's round directory is now WRITTEN as well as read:
    :mod:`.feature_classifier` files :data:`CLASSIFICATION_ARTIFACT` there, and
    a producer that located that directory by its own glob could file an
    artifact where this reader does not look. One rule, both directions.
    """
    matches = sorted(
        path for path in session_dir.glob(_EVIDENCE_GLOB) if path.is_dir()
    )
    if not matches:
        return None, NO_ROUND_ARTIFACTS_REASON
    if len(matches) > 1:
        # Fail closed rather than pick. Two round directories in one bundle
        # means the caller has to say which round it is asking about, and
        # guessing would silently grade a proposal against the wrong one.
        names = ", ".join(path.name for path in matches)
        return None, f"bundle carries more than one round ({names})"
    return matches[0], ""


def round_program_dir(
    session_dir: Path, round_dir: Path, phases: Iterable[str]
) -> Path:
    """Where this round's ``<phase>_program.wav`` files actually live, for
    the given ``phases``.

    Two shapes, tried in order and chosen by structure alone, never a flag.
    ``round_dir`` (:func:`round_artifact_dir`'s own return value) is tried
    first and wins whenever it holds ANY of ``phases``' program WAVs — so a
    round genuinely missing one of them still gets its own honest refusal
    from whichever caller asked, rather than being quietly rescued by an
    unrelated directory that happens to sit beside it.

    That "beside" directory is not hypothetical: the product's OWN sole
    producer of these files (``_play`` in
    :mod:`jasper.web.correction_crossover_v2`) writes every one of them to
    ``<session_dir>/crossover_v2/<relay>/`` — a SIBLING of ``evidence/``,
    never inside it. A tree-wide sweep of a real banked round confirms
    ``evidence/v1/artifacts/`` never carries a single ``*_program.wav``; the
    shape ``round_dir`` alone was built to read is one this instrument's own
    fixtures assumed but the product has never actually produced. Both
    :mod:`jasper.cli.classify_features` and
    :func:`~.round_views._find_program_wav` share this one rule so the
    location fact cannot drift between them again.
    """
    phases = tuple(phases)
    if any((round_dir / f"{phase}_program.wav").is_file() for phase in phases):
        return round_dir
    sibling = session_dir / "crossover_v2" / round_dir.name
    if any((sibling / f"{phase}_program.wav").is_file() for phase in phases):
        return sibling
    return round_dir


def _positions_block(cloud: dict[str, Any]) -> dict[str, Any]:
    """Per-position curves and capture integrity, copied rather than derived.

    The grid, the curves and the flat reference all come from ONE artifact, so
    a reader (and :func:`~.blend_prescription.positional_support`) cannot end
    up comparing a curve from one evaluation against a reference from another.
    No re-smoothing and no re-derivation: the lab's own per-position readers
    held that invariant and it is why their numbers could be trusted beside the
    round's.
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
    return {
        "available": bool(rows),
        "schema": positions.get("schema"),
        "n_positions": len(rows),
        "curve_grid": {
            "freqs_hz": grid.get("freqs_hz") or [],
            "fractional_octave": grid.get("fractional_octave"),
            "smoothing_fraction": grid.get("smoothing_fraction"),
            "floor_hz": grid.get("floor_hz"),
            "floor_source": grid.get("floor_source"),
        },
        "positions": rows,
        "redacted_fields": sorted(withheld),
        # The one fact a reader would otherwise assume was simply uninteresting.
        "angle_deg": {
            "status": "not_evaluated",
            "reason": (
                "no numeric microphone angle is banked anywhere in a round's "
                "artifacts; only the coarse role below is. Recovering an angle "
                "means reading the walk driver's own log, which is not part of "
                "this bundle."
            ),
        },
        "role_vocabulary": sorted({
            str(row.get("role")) for row in rows if row.get("role")
        }),
    }


def _region_block(receipt: dict[str, Any], reason: str) -> dict[str, Any]:
    """The crossover region a proposal must sit inside.

    ``round_measurements.blend.band_hz`` and nothing else. That field is the
    VERIFY absolute claim's own band, which decision 10 also makes the region
    the blend correction is solved and graded over — so a prescription checked
    against it is checked against byte-identically the band the deterministic
    solver was bounded by, rather than against a second derivation of "the
    crossover region" that could drift from it.
    """
    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    band = blend.get("band_hz")
    absent = _absence(reason, band is not None, "round_measurements.blend.band_hz")
    if absent:
        return {"available": False, **absent}
    return {
        "available": True,
        "band_hz": band,
        "source": "round_receipt.round_measurements.blend.band_hz",
        "note": (
            "the VERIFY absolute claim's band, which is also the region the "
            "deterministic blend correction is solved and graded over"
        ),
    }


def _incumbent_block(
    receipt: dict[str, Any], reason: str, state: dict[str, Any], state_reason: str
) -> dict[str, Any]:
    """What the measurement was taken THROUGH, from both places that record it.

    Two records of one fact, deliberately reported side by side rather than
    reconciled here: the receipt's ``round_measurements.blend.incumbent`` (what
    the round said it derived from) and the applied profile's own
    ``blend_correction`` (what the graph actually carried). They should agree,
    and a packet that silently preferred one would hide the round where they
    did not. Reconciling them is a judgement, and this module makes none.
    """
    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    profile = _mapping(state.get("pre_apply_profile"))
    from_receipt = blend.get("incumbent")
    from_profile = profile.get("blend_correction")
    return {
        "from_round_receipt": (
            from_receipt
            if from_receipt is not None
            else _absence(reason, False, "round_measurements.blend.incumbent")
        ),
        "from_applied_profile": (
            from_profile
            if from_profile is not None
            else _absence(
                state_reason,
                bool(profile),
                "pre_apply_profile.blend_correction",
            )
        ),
        "note": (
            "a prescription is a TOTAL, not a delta: prescribe the whole "
            "correction the next round should apply, incumbent included"
        ),
    }


def _verify_block(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Per-claim verdicts, copied verbatim including their ``not_evaluated``.

    These live only in the flow state, never in the bundle — the receipt's own
    ``verification`` block is a different, coarser record. The per-claim
    ``status``/``reason`` pairs are the honest ones (``not_evaluated`` +
    ``no_per_branch_verify_capture`` is the shipped answer for both per-branch
    claims on every round in the corpus), and they are passed through
    untouched.
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

    Read from the design draft's confirmed ``driver_safety_profile``, which is
    where a speaker's per-driver declarations already live and are already
    gated. Composed by :func:`~.driver_prescription.driver_passbands_from_safety_profile`
    rather than here, so the packet reports a band it does not also define — the
    same split every other block in this module keeps.

    Deliberately NOT derived from the crossover: ``branch_chain.
    radiating_band_hz`` would give the band this driver is within 3 dB of full
    output over, which is the bound on a LIFT and is narrower than the driver.
    The owner's directive is that the whole driver be correctable, and a cut
    past the handoff is ordinary useful work.
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


def _lab_row_value(value: Any, column: str, non_finite: set[str]) -> Any:
    """One lab-row value as exact JSON, naming any number that was not.

    A classification row legitimately carries ``NaN``. The instrument writes one
    for ``z_local`` when a feature's neighbourhood scatter is zero, for
    ``frac_of_nmp`` when the control scale is, and into ``excess_loss_vs_null``
    when a gate's reference reading is — and ``jasper-classify-features`` banks
    the artifact with a plain ``json.dumps``, which writes ``NaN`` verbatim and
    reads it back as a float.
    :func:`~jasper.audio_measurement.evidence_identity.json_fingerprint` refuses
    a non-finite number, so copying one through would leave a round that
    classified perfectly well with NO packet at all: this module's one hard
    failure, thrown for a value that is merely absent.

    So a non-finite number becomes ``null`` — the same answer
    :func:`~.feature_classification.read_feature_verdicts` already gives for one
    — and its COLUMN is named in the block's ``non_finite_fields``, because
    "not computable" and "not carried" are different facts and the packet's
    rule is that neither is silently the other. Recursive because three columns
    are per-gate tables and one is a list, not scalars.

    The four branches are exactly what ``json.loads`` can produce that
    ``_freeze_json`` cares about, and no more. There is deliberately no ``bool``
    guard: ``bool`` subclasses ``int``, never ``float``, so a boolean column
    (``clean``, ``is_dip``, ``controls_ok``) falls through to the passthrough
    already — unlike in :func:`~.feature_classification._finite`, which needs one
    because its check includes ``int``.

    Deliberately scoped to the lab rows, which is where the failure was
    observed. The sibling exposure is real but NARROW, and was measured rather
    than assumed: the receipt, the cloud evidence and the finding set are banked
    through :func:`~jasper.active_speaker.commissioning_evidence_store._canonical_json`,
    which passes ``allow_nan=False`` and refuses a non-finite value at write
    time, so they structurally cannot carry one here. Two inputs are written
    with a plain ``json.dumps`` and can:
    ``save_v2_state`` (:mod:`jasper.web.correction_crossover_v2`) for the flow
    state, where all four fields this packet copies —
    ``verify.claims``, ``fc_selection``, ``pre_apply_profile.blend_correction``
    and ``evidence.calibration`` — kill the packet; and
    :func:`~jasper.active_speaker.design_draft.save_design_draft`, whose
    ``driver_safety_profile.confirmation`` is copied whole and does the same.
    The draft's passbands do NOT, because
    :func:`~.driver_prescription.driver_passbands_from_safety_profile` already
    drops a non-finite bound. Repairing those two is their writers' change, not
    this reader's — see the follow-up issue linked from PR #2833.
    """
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        non_finite.add(column)
        return None
    if isinstance(value, dict):
        return {
            key: _lab_row_value(item, column, non_finite)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_lab_row_value(item, column, non_finite) for item in value]
    return value


def _classification_block(raw: Any, reason: str) -> dict[str, Any]:
    """The banked feature verdicts and the working behind them, not re-derived.

    TWO views of one artifact, side by side and deliberately not joined.

    ``verdicts`` is the gate's: copied through
    :func:`~.feature_classification.read_feature_verdicts`, which drops a row it
    cannot type rather than admitting it as ``ambiguous`` — so the count this
    block reports is the count a gate can actually use, and a half-readable
    artifact cannot look fuller than it is. ``n_rows_banked`` beside it is the
    raw count, because a denominator that moved silently is a different
    measurement wearing the same number.

    ``lab_rows`` is the artifact's own: every banked row object, copied field by
    field through :data:`~.feature_classification.LAB_ROW_FIELDS` on this
    module's ordinary allowlist rule, with the names of anything held back
    published as ``redacted_fields`` and of any column that was not exact JSON
    as ``non_finite_fields``. It carries the working a gate must not
    act on but a READER of this packet needs to audit a verdict — how far the
    excursion sat from a real cancellation's scale, what each shorter gate did
    to the feature, which gates resolved it. Rows the typed reader dropped keep
    their working here, which is how a reader sees WHY one was dropped; they
    reach no gate, because no gate reads this key.

    Not joined into one list per feature on purpose: the typed reader drops
    rows, so the two lists do not line up by index, and pairing them by
    frequency is a judgement about which row a verdict came from that this
    module does not make.

    ``uncertainty`` labels every spread the rows publish. Each is ``random`` or
    ``systematic`` and says what it is a spread of, and the two columns that
    merely LOOK like uncertainties say why they are not — ``gate_slack`` most of
    all, since it is the larger of a fixed floor and a random 3-sigma and would
    pool the two kinds in one figure if it were read as one.
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
            column: _lab_row_value(value, column, non_finite)
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


def _not_evaluated(
    *,
    receipt_reason: str,
    cloud_reason: str,
    state_reason: str,
    classification_available: bool,
    drivers_available: bool,
    findings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Everything this packet could not answer, and why — one honest list.

    A reader that scans only the fields present will draw conclusions from a
    shape it cannot see the edges of. This block is the edges. Entries whose
    absence is a property of the CORPUS (nothing banks a distortion reading)
    are stated whether or not this particular session was complete.
    """
    entries: list[dict[str, Any]] = [
        {
            "field": "positions[].angle_deg",
            "reason": (
                "no numeric microphone angle is banked; only the coarse "
                "onax/offax role is"
            ),
        },
        {
            "field": "first_reflection_ms",
            "reason": (
                "the reflection time is narrated inside verify.gate.disclosure "
                "prose and is not banked as a number anywhere in a round's "
                "artifacts"
            ),
        },
        {
            "field": "harmonic_distortion",
            "reason": (
                "H2/H3 are computable from banked captures but no round writes "
                "them; there is no distortion record to carry"
            ),
        },
        # The one place the PRESCRIPTION PATH states this geometry — the remote
        # tier's own disclosures (``crossover_v2_flow.REMOTE_VERTICAL_DISCLOSURE``,
        # ``crossover_envelope_v2._REMOTE_VERTICAL_NUDGE``) carry theirs, about
        # their own artifacts. It is a property of the CORPUS — every shape a
        # round banks is horizontal, whoever wrote the artifact — so it belongs
        # here rather than as a per-row flag two producers spell differently
        # (#2783). It DISCLOSES; it refuses nothing: the owner's 2026-08-21
        # ruling opened the boost door on exactly this risk, which is a
        # correction that may not generalise off-axis vertically — reversible
        # and measurable — and not a component-safety one.
        {
            "field": "vertical_plane_response",
            "reason": (
                "every capture shape a round banks is horizontal — a turntable "
                "walk swings at fixed height and radius, a position cloud is a "
                "floor-plan of seats — so no banked evidence sees a floor or "
                "ceiling bounce, and what a filter of either sign does off the "
                "horizontal plane is unmeasured rather than shown to be safe"
            ),
        },
    ]
    if not classification_available:
        # Was unconditional until a round could carry banked verdicts. Stating
        # it while the packet carries verdicts would be the opposite of the
        # honesty this block exists for: "we did not look" printed beside the
        # thing we looked at.
        entries.append({
            "field": "per_bin_minimum_phase_class",
            "reason": (
                "no feature classification is banked for this round. The "
                "instrument that produces one runs offline over a round's "
                "banked captures (jasper-classify-features) and nobody ran it "
                "here. The positional bar in the response format is the "
                "deterministic stand-in for the BLEND boost class; the "
                "per-driver class refuses, either sign, rather than standing in"
            ),
        })
    if not drivers_available:
        entries.append({
            "field": "drivers.passbands_hz",
            "reason": (
                "no confirmed driver-safety profile was supplied, so this "
                "packet cannot say where each driver's own band starts and "
                "ends; a per-driver prescription has no bound to be checked "
                "against and is refused"
            ),
        })
    if receipt_reason:
        entries.append({"field": "round_receipt", "reason": receipt_reason})
    if cloud_reason:
        entries.append({"field": "cloud_verify", "reason": cloud_reason})
    if state_reason:
        entries.append({
            "field": "flow_state",
            "reason": (
                f"{state_reason}; fc_selection, per-claim verify verdicts and "
                "the applied profile live only here"
            ),
        })
    if isinstance(findings.get("findings"), list) and not findings["findings"]:
        entries.append({
            "field": "findings",
            "reason": (
                "the finding set was produced and is empty — no attributed "
                "finding was promoted for this round"
            ),
        })
    return entries


def build_crossover_evidence_packet(
    session_dir: Path,
    *,
    state_path: Path | None = None,
    driver_draft_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one round's banked evidence into one versioned document.

    ``session_dir`` is a commissioning bundle: an ``info.json`` beside an
    ``evidence/v1/artifacts/crossover_v2/<relay-session-id>/`` directory
    holding the round receipt, the cloud evidence, the finding set and the
    per-position records.

    ``state_path`` is the flow state file (``jts_crossover_v2_flow_state``),
    which is banked separately because the bundle does not contain it. It is
    OPTIONAL and its absence is reported rather than papered over — but a
    packet without it cannot carry the per-claim verify verdicts, the Fc
    selection, or the applied profile's own incumbent, and says so in the
    packet's ``not_evaluated`` block.

    ``driver_draft_path`` is the active-speaker design draft
    (``active_speaker_design_draft.json``), which carries the confirmed
    driver-safety profile and is likewise banked outside the bundle. Same
    posture and same reason: OPTIONAL, absence reported. A packet without it
    cannot say where each driver's own band starts and ends, so the per-driver
    prescription class has no bound to be checked against and refuses by name.

    Raises :class:`CrossoverEvidencePacketError` only when ``session_dir`` is
    not a crossover-v2 session bundle at all. Every other missing or
    unreadable artifact is reported inside the packet, because a partially
    banked round is a normal thing to want to read.
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
    findings_raw, _ = _read_json(round_dir / "findings_cloud_verify.json")
    classification_raw, classification_reason = _read_json(
        round_dir / CLASSIFICATION_ARTIFACT
    )
    receipt = _mapping(receipt_raw)
    cloud = _mapping(cloud_raw)
    findings = _mapping(findings_raw)

    state_raw: Any = None
    state_reason = "no flow state file was supplied"
    if state_path is not None:
        state_raw, read_reason = _read_json(state_path)
        state_reason = read_reason
    state = _mapping(state_raw)
    state_withheld = sorted(key for key in _STATE_WITHHELD if key in state)

    draft_raw: Any = None
    draft_reason = "no driver design draft was supplied"
    if driver_draft_path is not None:
        draft_raw, read_reason = _read_json(driver_draft_path)
        draft_reason = read_reason
    drivers = _drivers_block(_mapping(draft_raw), draft_reason)
    classification = _classification_block(classification_raw, classification_reason)

    identity, identity_withheld = _copy_allowed(
        _mapping(info_raw.get("fingerprints")), _IDENTITY_FIELDS
    )
    spec = _mapping(cloud.get("spec"))

    packet: dict[str, Any] = {
        "artifact_schema_version": PACKET_SCHEMA_VERSION,
        "kind": PACKET_KIND,
        "generated_by": GENERATED_BY,
        "privacy": {
            "raw_audio_excluded": True,
            "absolute_paths_excluded": True,
            "household_prose_excluded": True,
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
            "relay_session_id": round_dir.name,
            "state": info_raw.get("state"),
            "started_at": info_raw.get("started_at"),
            "round_id": receipt.get("round_id"),
            "note": (
                "bundle_session_id and relay_session_id are different id "
                "namespaces; the round artifacts are filed under the relay id"
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
        "crossover_region": _region_block(receipt, receipt_reason),
        "incumbent": _incumbent_block(receipt, receipt_reason, state, state_reason),
        # Verbatim, every one of them. `spec.bands[]` carries `evaluable`,
        # `n_excluded` and `graded_lo_hz`; `flatness` carries `n_excluded` and
        # `evaluable`. Those are the fields that say how much of the band the
        # number actually covers, and a packet that summarised over them would
        # be easier to misread than the tools that print them.
        "flatness": _mapping(cloud.get("flatness")),
        "spec": spec,
        "curve": _mapping(cloud.get("curve")),
        "positions": _positions_block(cloud),
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
        "findings": {
            "findings": findings.get("findings") or [],
            "field_descriptions": _mapping(findings.get("field_descriptions")),
        },
        "verify": _verify_block(state, state_reason),
        "fc_selection": (
            _mapping(state.get("fc_selection"))
            if isinstance(state.get("fc_selection"), dict)
            else _absence(state_reason, False, "fc_selection")
        ),
        # The two per-DRIVER evidence blocks. They travel together because a
        # per-driver prescription needs both to be checked at all: the band
        # says where a filter may sit, the verdicts say what it may be aimed
        # at, and either alone answers half the question.
        "drivers": drivers,
        "feature_classification": classification,
        "not_evaluated": _not_evaluated(
            receipt_reason=receipt_reason,
            cloud_reason=cloud_reason,
            state_reason=state_reason,
            classification_available=bool(classification.get("available")),
            drivers_available=bool(drivers.get("available")),
            findings=findings,
        ),
        # TWO contracts, one per prescription class, each written by the gate
        # that enforces it. Beside each other rather than merged: they describe
        # different shapes with different bounds, and a merged block would need
        # an owner that is neither gate.
        "response_format": prescription_response_format(),
        "driver_response_format": driver_prescription_response_format(),
        # …and the doors this one does NOT open (#2773). A reader who found
        # only the two contracts above would conclude that two things can be
        # prescribed for a round, when four can — the other two arrive as
        # request-body keys at session open and refuse the whole session rather
        # than just the staging. Named apart from the two above rather than
        # listed beside them precisely because they are not stageable: a
        # document of this class handed to ``stage`` is refused by the class
        # gate, and the block says where it belongs instead.
        "request_time_prescriptions": {
            "alignment": alignment_prescription_response_format(),
            "topology": topology_prescription_response_format(),
        },
    }
    packet["packet_fingerprint"] = _fingerprint(packet)
    return packet


def _fingerprint(packet: dict[str, Any]) -> str:
    """The content hash a prescription must echo back.

    Through :func:`~jasper.audio_measurement.evidence_identity.json_fingerprint`
    — this repository's one content hash — over the packet MINUS the
    fingerprint field itself, which does not exist yet at this point. A
    prescription naming this digest can only have been written against this
    exact evidence.
    """
    try:
        return json_fingerprint(packet, field_name="evidence_packet")
    except EvidenceIdentityError as exc:  # pragma: no cover - defensive
        raise CrossoverEvidencePacketError(
            f"packet is not exact JSON data: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# the two readers the gate uses — named here so the packet owns its own shape
# --------------------------------------------------------------------------- #


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
