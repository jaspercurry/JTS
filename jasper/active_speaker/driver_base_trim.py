# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured per-driver BASE TRIM — one writer, one reader.

The relative level a driver needs so the acoustic sum is level across every
declared crossover. Until this artifact exists a speaker ships the trim
``baseline_profile._derive_corrections`` derives from the drivers' DECLARED
sensitivities, which is a datasheet claim about some other cabinet: on
2026-08-23 a DE250 rated on B&C's ME45 horn but installed on an R-OSSE
waveguide seeded a −10.8 dB tweeter trim nobody had measured. This module is
where that estimate becomes an observation.

Ownership, deliberately narrow:

* **one writer** — :mod:`jasper.cli.driver_trim`, after per-driver captures
  analysed through :func:`~jasper.active_speaker.driver_acoustics.analyze_driver_capture`;
* **one reader** — ``baseline_profile._measured_level_trims``, which prefers a
  banked base trim over the guided-capture derivation and falls back to it;
* **absent is normal.** A speaker that has never run the trim step behaves
  exactly as it did before this module existed — the guided captures, then the
  datasheet estimate.

**No estimator lives here.** The per-driver level is
``driver_acoustics._overlap_band_levels``' mean deconvolved magnitude over the
one-octave band log-symmetric about the declared Fc, and the chain from
adjacent-driver deltas to a per-role attenuation is
``level_trim.attenuation_from_group_deltas``. Both already ship and both are
already what the profile consumes; this module only banks their answer. Minting
a third way to measure an inter-driver level gap is the defect
``intervention.compare_level_definitions`` and ``baseline_profile._estimator_cross_check``
exist to disclose, and it is not reopened here.

**Re-keying is a loud refusal, never a migration.** The record names the
declaration it was measured against (the crossover preview's own fingerprint).
A speaker whose declaration has moved — a different Fc, a different driver, a
re-save at ``/sound`` — has a banked trim that answers a question nobody is
asking any more, so the reader refuses it with one remediation ("re-run
``jasper-driver-trim``") and falls back. Under the owner's 2026-08-23
no-legacy-config ruling this file grows no tolerant readers for older stored
shapes: a breaking change to the payload refuses the same way, and re-measuring
is the accepted cost.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_json

from .level_trim import (
    MAX_ATTENUATION_DB,
    LevelTrimError,
    attenuation_from_group_deltas,
)

SCHEMA_VERSION = 1
BASE_TRIM_KIND = "jts_active_speaker_driver_base_trim"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_driver_base_trim.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_DRIVER_BASE_TRIM_STATE"

#: What the reader did with the banked record, for the level-match ledger.
STATUS_ABSENT = "absent"
STATUS_APPLIED = "applied"
STATUS_DECLARATION_CHANGED = "declaration_changed"
STATUS_ROLES_CHANGED = "roles_changed"
STATUS_UNUSABLE = "unusable"

#: The statuses that mean "a trim was banked and this speaker is NOT using it".
#: ``absent`` is not one of them — a box that never measured is the ordinary
#: case — but every member here is a measurement being discarded, which the
#: profile must say out loud rather than leaving indistinguishable from never
#: having measured at all.
REFUSED_STATUSES = frozenset({
    STATUS_DECLARATION_CHANGED,
    STATUS_ROLES_CHANGED,
    STATUS_UNUSABLE,
})

#: The single remediation string. One sentence, one verb, in every surface that
#: refuses a banked trim, so an operator never has to reconcile two wordings.
REMEASURE_REMEDIATION = "re-run jasper-driver-trim to measure this speaker again"


class DriverBaseTrimError(ValueError):
    """A base-trim record was asked to be written outside its own envelope."""


def base_trim_state_path(path: str | Path | None = None) -> Path:
    """Where the base trim lives: an explicit path, the env override, or the
    default. One resolver, so every surface probes the file the reader reads."""
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fc_key(fc: float) -> str:
    """How a declared Fc is spelled as a JSON key. One writer, one reader: the
    record's evidence is keyed this way and the reader re-derives which groups
    it covers by looking the same keys back up."""
    return f"{float(fc):g}"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _levelled_group_ids(
    levels: Mapping[str, Any], crossovers: Any
) -> list[str]:
    """The groups the record actually levelled: those carrying a finite level
    for BOTH drivers of EVERY crossover it names.

    The same completeness rule :func:`solve_base_trims` applies before a group
    contributes a delta chain, re-derived from the banked evidence so the set
    the readiness gate reads is measured rather than asserted. An unreadable
    ``crossovers`` list yields no group, which is the fail-closed direction:
    the caller falls back to the trim it already had.
    """
    required: list[tuple[str, str, str]] = []
    for region in crossovers if isinstance(crossovers, list) else ():
        if not isinstance(region, Mapping):
            return []
        lower = str(region.get("lower_role") or "")
        upper = str(region.get("upper_role") or "")
        fc = _finite(region.get("declared_fc_hz"))
        if not lower or not upper or fc is None:
            return []
        required.append((lower, upper, _fc_key(fc)))
    if not required:
        return []
    levelled: list[str] = []
    for group_id, by_role in levels.items():
        if not str(group_id) or not isinstance(by_role, Mapping):
            continue
        if all(
            isinstance(by_role.get(role), Mapping)
            and _finite(by_role[role].get(fc_key)) is not None
            for lower, upper, fc_key in required
            for role in (lower, upper)
        ):
            levelled.append(str(group_id))
    return sorted(levelled)


def _mixed_geometry_group_ids(geometries: Mapping[str, Any]) -> list[str]:
    """Groups whose drivers were NOT all captured under one geometry.

    Disclosed, never refused. The delta between two drivers read at one mic
    position is what the trim is built on, and reading one near-field and the
    other on the reference axis compares two different acoustic distances — a
    small effect, but one nothing in the record otherwise says out loud.
    """
    mixed: list[str] = []
    for group_id, by_role in geometries.items():
        if not str(group_id) or not isinstance(by_role, Mapping):
            continue
        if len({str(value) for value in by_role.values()}) > 1:
            mixed.append(str(group_id))
    return sorted(mixed)


def solve_base_trims(
    levels_db: Mapping[str, Mapping[str, Mapping[float, float]]],
    roles: Sequence[str],
    regions: Sequence[tuple[str, str, float]],
) -> dict[str, float]:
    """Per-role attenuation from measured, ledger-normalized overlap levels.

    ``levels_db`` is ``speaker_group_id -> role -> declared Fc -> level``, where
    each level is the overlap-band level MINUS that capture's own effective
    excitation peak — the excitation-ledger normalize that makes captures taken
    at different protected drive levels comparable, exactly as
    ``baseline_profile._measured_level_trims`` does it.

    Fail-closed in one direction only: a speaker group missing a usable level
    for either driver of any declared crossover is DROPPED, and no qualifying
    group returns ``{}`` so the caller keeps the estimate it already had. The
    surviving groups are averaged and normalized up by
    :func:`~jasper.active_speaker.level_trim.attenuation_from_group_deltas`:
    every trim is an attenuation, and the vector is shifted so its maximum is
    exactly 0 dB — the QUIETEST driver is the reference and nothing is
    attenuated further than the level match requires, which is the give-back
    the composed gain structure owes the speaker's maximum SPL.
    """
    ordered = tuple(roles)
    chains: list[list[tuple[str, str, float]]] = []
    for group_id in sorted(levels_db):
        by_role = levels_db[group_id]
        if not isinstance(by_role, Mapping):
            continue
        deltas: list[tuple[str, str, float]] = []
        for lower, upper, fc in regions:
            lower_levels = by_role.get(lower)
            upper_levels = by_role.get(upper)
            level_lo = (
                _finite(lower_levels.get(fc))
                if isinstance(lower_levels, Mapping)
                else None
            )
            level_up = (
                _finite(upper_levels.get(fc))
                if isinstance(upper_levels, Mapping)
                else None
            )
            if level_lo is None or level_up is None:
                deltas = []
                break
            deltas.append((lower, upper, level_up - level_lo))
        if deltas:
            chains.append(deltas)
    if not chains:
        return {}
    try:
        return attenuation_from_group_deltas(
            ordered, chains, minimum_db=MAX_ATTENUATION_DB
        )
    except LevelTrimError:
        return {}


def load_base_trim(*, state_path: str | Path | None = None) -> dict[str, Any] | None:
    """The persisted record, or ``None`` when there is none to read.

    Absent-tolerant and never raises: an unreadable, malformed, wrong-kind, or
    wrong-schema file is indistinguishable from no file at all, because the
    consumer's fallback — the guided captures, then the datasheet estimate — is
    the conservative answer in every one of those cases.
    """
    path = base_trim_state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != BASE_TRIM_KIND
        or raw.get("artifact_schema_version") != SCHEMA_VERSION
    ):
        return None
    return raw


def banked_base_trims(
    declaration_fingerprint: str | None,
    roles: Sequence[str],
    *,
    state_path: str | Path | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The banked trim for THIS declaration, or ``({}, why-not)``.

    Every rejection is reported rather than swallowed: a banked trim that is
    silently ignored looks exactly like a speaker that was never measured, and
    the operator would have no way to tell the two apart.

    A caller that hands over NO declaration gets
    :data:`STATUS_DECLARATION_CHANGED` too, sharing the status because it earns
    the same answer: a record that cannot be keyed to the declaration in front
    of us is not evidence about this speaker.

    The trims are re-validated against the writer's own envelope on the way out
    (finite, attenuation-only, at or above
    :data:`~jasper.active_speaker.level_trim.MAX_ATTENUATION_DB`). That envelope
    is relative to UNITY and that is the whole of its guarantee: no accepted
    trim is a boost, and none is deeper than the floor the solver clamps to. It
    says nothing about the datasheet estimate the record replaces — a
    hand-edited tweeter trim of −5.0 dB where the estimate held −10.8 dB is
    inside the envelope and runs that driver 5.8 dB louder than the unmeasured
    speaker would have.
    """
    ordered = tuple(roles)
    record = load_base_trim(state_path=state_path)
    if record is None:
        return {}, {"status": STATUS_ABSENT}
    banked_fingerprint = record.get("declaration_fingerprint")
    meta: dict[str, Any] = {
        "measured_at": record.get("measured_at"),
        "declaration_fingerprint": banked_fingerprint,
        "state_path": str(base_trim_state_path(state_path)),
    }
    if (
        not isinstance(declaration_fingerprint, str)
        or not declaration_fingerprint
        or banked_fingerprint != declaration_fingerprint
    ):
        return {}, {
            **meta,
            "status": STATUS_DECLARATION_CHANGED,
            "expected_declaration_fingerprint": declaration_fingerprint,
            "remediation": REMEASURE_REMEDIATION,
        }
    raw_trims = record.get("trims_db")
    if not isinstance(raw_trims, Mapping) or set(raw_trims) != set(ordered):
        return {}, {
            **meta,
            "status": STATUS_ROLES_CHANGED,
            "roles": sorted(ordered),
            "remediation": REMEASURE_REMEDIATION,
        }
    trims: dict[str, float] = {}
    for role in ordered:
        value = _finite(raw_trims.get(role))
        if value is None or value > 0.0 or value < MAX_ATTENUATION_DB:
            return {}, {
                **meta,
                "status": STATUS_UNUSABLE,
                "detail": f"{role} trim is outside the attenuation-only envelope",
                "remediation": REMEASURE_REMEDIATION,
            }
        trims[role] = value
    levels = record.get("levels_db")
    geometries = record.get("capture_geometries")
    if not isinstance(levels, Mapping) or not isinstance(geometries, Mapping):
        return {}, {
            **meta,
            "status": STATUS_UNUSABLE,
            "detail": "the record carries no readable per-driver evidence",
            "remediation": REMEASURE_REMEDIATION,
        }
    measured = _levelled_group_ids(levels, record.get("crossovers"))
    if not measured:
        return {}, {
            **meta,
            "status": STATUS_UNUSABLE,
            "detail": (
                "no speaker group in the record carries a level for both "
                "drivers of every declared crossover"
            ),
            "remediation": REMEASURE_REMEDIATION,
        }
    return trims, {
        **meta,
        "status": STATUS_APPLIED,
        "trims": dict(trims),
        # WHICH speaker groups this trim was measured on, not merely how many,
        # and DERIVED from the record's own evidence rather than read off the
        # bare keys of ``levels_db``: the record banks every group it captured,
        # including one the solve dropped for a driver it never got a level for.
        # ``crossover_contract.automatic_candidate_readiness`` gates on the
        # measured-group SET against the topology's required one, so a record
        # that levelled only the left cabinet of a stereo pair must not read as
        # having levelled both.
        "speaker_group_ids": measured,
        "groups_total": sum(1 for group_id in levels if str(group_id)),
        "mixed_geometry_group_ids": _mixed_geometry_group_ids(geometries),
    }


def write_base_trim(
    *,
    trims_db: Mapping[str, float],
    levels_db: Mapping[str, Mapping[str, Mapping[float, float]]],
    capture_geometries: Mapping[str, Mapping[str, str]],
    roles: Sequence[str],
    regions: Sequence[tuple[str, str, float]],
    declaration_fingerprint: str,
    microphone: Mapping[str, Any],
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Publish one measured base trim. Called ONLY on a complete solve.

    Raises :class:`DriverBaseTrimError` when the record would not survive
    :func:`banked_base_trims` — writing a value the reader rejects is a silent
    no-op dressed up as success.
    """
    ordered = tuple(roles)
    if not isinstance(declaration_fingerprint, str) or not declaration_fingerprint:
        raise DriverBaseTrimError("a base trim must name the declaration it measured")
    if set(trims_db) != set(ordered):
        raise DriverBaseTrimError(
            f"trims cover {sorted(trims_db)!r}, not the declared roles "
            f"{sorted(ordered)!r}"
        )
    trims: dict[str, float] = {}
    for role in ordered:
        value = _finite(trims_db.get(role))
        if value is None or value > 0.0 or value < MAX_ATTENUATION_DB:
            raise DriverBaseTrimError(
                f"{role} trim {trims_db.get(role)!r} dB is outside the "
                f"({MAX_ATTENUATION_DB:g}, 0.0] dB attenuation-only envelope"
            )
        trims[role] = round(value, 1)
    path = base_trim_state_path(state_path)
    # The evidence behind the trims, so a reader can re-derive them without the
    # WAVs: one ledger-normalized level per group, per role, per declared Fc.
    # Fc keys are stringified because JSON has no float key.
    banked_levels = {
        group_id: {
            role: {
                _fc_key(fc): round(float(level), 2)
                for fc, level in sorted(by_fc.items())
            }
            for role, by_fc in sorted(by_role.items())
        }
        for group_id, by_role in sorted(levels_db.items())
    }
    banked_crossovers = [
        {"lower_role": lower, "upper_role": upper, "declared_fc_hz": float(fc)}
        for lower, upper, fc in regions
    ]
    if not _levelled_group_ids(banked_levels, banked_crossovers):
        raise DriverBaseTrimError(
            "no speaker group carries a level for both drivers of every declared "
            "crossover, so this record's own reader would refuse it"
        )
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASE_TRIM_KIND,
        "measured_at": _utc_now(),
        "state_path": str(path),
        "declaration_fingerprint": declaration_fingerprint,
        "roles": list(ordered),
        "trims_db": trims,
        "levels_db": banked_levels,
        "crossovers": banked_crossovers,
        "microphone": dict(microphone),
        # Under WHICH geometry each driver was read, per group. The trim is a
        # delta between two drivers, so two drivers read at different acoustic
        # distances is a fact about the answer's footing; the reader discloses
        # it as ``mixed_geometry_group_ids`` rather than guessing it away.
        "capture_geometries": {
            group_id: {role: str(geometry) for role, geometry in sorted(by_role.items())}
            for group_id, by_role in sorted(capture_geometries.items())
        },
        # Named so the absence reads as a decision, not an oversight: one mic
        # position per driver measures the ON-AXIS ratio, and a waveguide's
        # on-axis level overstates its power output against a cone's. The
        # listening-window average is the arm-walk program's question.
        "geometry_claim": "on_axis_single_position",
    }
    atomic_write_json(path, payload, mode=0o640, group_from_parent=True)
    return payload
