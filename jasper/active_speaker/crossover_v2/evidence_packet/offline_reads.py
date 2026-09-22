# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..feature_classification import (
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    read_feature_verdicts,
)
from ..journey import PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_MEASURE
from ..prescription_contract import CONTRACT_COMMAND

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

#: :func:`~jasper.active_speaker.round_bank.bank_round` owns the sidecar/WAV layout.
#: ``**/`` also admits older pulled rings with a directory per phase. Both
#: :func:`~.feature_classifier.load_round_captures` and
#: :func:`~.harmonic_evidence.read_round_harmonics` use this ring-root pattern.
RING_SIDECAR_GLOB = "**/sidecar/*.json"


def _read_json(path: Path) -> tuple[Any, str]:
    """One artifact, or the reason it is absent or unreadable."""
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
