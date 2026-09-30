# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import unavailable
from jasper.platform.json_fields import as_mapping

from ..feature_classification import (
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    read_feature_verdicts,
)
from ..prescription_contract import CONTRACT_COMMAND
from ..round_inputs import RoundInputs, view_path

#: The ``jasper-round-views classify-features`` artifact
#: (:mod:`.feature_classifier`): one name shared by that view, this packet, and
#: the gate that acts on it. No stage of a round writes it — it is an offline
#: view — so its absence is an ordinary reported ``source_absent``.
CLASSIFICATION_ARTIFACT = "feature_classification.json"

#: The ``jasper-round-views distortion`` artifact (:mod:`.harmonic_evidence`),
#: same posture: the packet owns the names of what it reads.
HARMONICS_ARTIFACT = "harmonic_distortion.json"


#: A file handed to the builder that could not be read or parsed.
SOURCE_UNREADABLE = "source_unreadable"
#: A readable document whose field holds the wrong type, which ``field_null`` would misname.
FIELD_MALFORMED = "field_malformed"


def read_json(path: Path) -> tuple[Any, str, str]:
    """One artifact, or the code for why it is absent or unreadable and the sentence behind it."""
    if not path.exists():
        return None, "source_absent", ""
    try:
        return json.loads(path.read_text()), "", ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, SOURCE_UNREADABLE, f"unreadable: {type(exc).__name__}"
    except json.JSONDecodeError as exc:
        return None, SOURCE_UNREADABLE, f"not valid JSON: {exc.msg}"


def absence(source_reason: str, present: bool, field: str, detail: str = "") -> dict[str, Any]:
    """Which absence this is, said as a code, with ``detail`` the sentence behind it.

    ``source_absent`` when the artifact never arrived, ``source_unreadable``
    when it arrived and could not be read, ``field_null`` when it was read and
    the field inside it is null. Merging them is the reading defect this
    packet exists partly to fix.
    """
    if source_reason or not present:
        return {**unavailable(source_reason or "field_null", detail or None), "field": field}
    return {}


def copy_allowed(
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


def exact_json_value(value: Any, column: str, non_finite: set[str]) -> Any:
    """One copied value as exact JSON, naming any column that was not.

    Two inputs legitimately carry ``NaN``: a classification row (the instrument
    writes one for ``z_local``, ``frac_of_nmp`` and ``excess_loss_vs_null``
    when the underlying scale is zero) and a take's ``diagnostic``. Both are banked
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
            key: exact_json_value(item, column, non_finite)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [exact_json_value(item, column, non_finite) for item in value]
    return value


def _harmonics_block(raw: Any, reason: str, detail: str = "") -> dict[str, Any]:
    """The round's banked H2/H3 reading, copied through with its declarations.

    Verbatim: the instrument that produced it (``jasper-round-views distortion``, over
    :mod:`.harmonic_evidence`) owns what the numbers mean. What this adds is
    the uncertainty declarations the artifact does not carry.

    The packet does not compute it: the view files beside the round
    (ADR-0346). Absence is ordinary and reported.
    """
    if not isinstance(raw, dict):
        return {
            # A file that PARSED into a non-object has no read reason.
            **unavailable(reason or "field_null", (detail or None) if reason else (
                f"the {HARMONICS_ARTIFACT} banked for this round parsed as "
                f"{type(raw).__name__}, not as a JSON object, so there is no "
                "reading in it to publish"
            )),
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
            **unavailable("field_null", (
                "a harmonic-distortion artifact is banked for this round but "
                "carries no role block, so there is no reading in it to publish"
            )),
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
            **unavailable("field_null", (
                "a harmonic-distortion artifact is banked for this round but "
                "names no harmonic order, so nothing says what its rows are "
                "readings OF and no column in them could be declared"
            )),
            "n_roles": 0,
        }
    captures = as_mapping(raw.get("captures"))
    return {
        "status": "available",
        "schema": raw.get("schema"),
        "orders": orders,
        "n_roles": len(roles),
        "roles": roles,
        # What the instrument could NOT read, beside what it could.
        "captures": captures,
        # Load-bearing rather than housekeeping: an uncalibrated read carries
        # the microphone's own response inside every ratio.
        "calibration": as_mapping(raw.get("calibration")),
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


def _classification_block(raw: Any, reason: str, detail: str = "") -> dict[str, Any]:
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
    this module does not make.

    ``uncertainty`` labels every spread the rows publish, and says why the two
    columns that merely LOOK like uncertainties are not — ``gate_slack`` most
    of all, a dB bar beside a dB reading rather than an error bar on it.
    """
    absent = absence(reason, raw is not None, CLASSIFICATION_ARTIFACT, detail)
    if absent:
        return {
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
        kept, dropped = copy_allowed(entry, LAB_ROW_FIELDS)
        withheld.update(dropped)
        lab_rows.append({
            column: exact_json_value(value, column, non_finite)
            for column, value in kept.items()
        })
    return {
        **({"status": "available"} if verdicts else unavailable("field_null")),
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


def _derived_views_block(inputs: RoundInputs) -> dict[str, Any]:
    """The classification and H2/H3 views, read beside the round."""
    return {
        "feature_classification": _classification_block(
            *read_json(view_path(inputs, CLASSIFICATION_ARTIFACT))
        ),
        "harmonics": _harmonics_block(*read_json(view_path(inputs, HARMONICS_ARTIFACT))),
    }
