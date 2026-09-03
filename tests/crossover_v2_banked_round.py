# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One banked round, as the FLOW banks it — the shared real-shape fixture.

A fixture library that IS a fixture library, on
``tests/crossover_v2_round_harness.py``'s precedent and for its reason: a
shared builder living in a collected test module makes that module
undeletable. This one is imported by the round-views and forward-model
suites.

**Every artifact here is written by the product's own writer.** The bundle is
:func:`~jasper.active_speaker.bundles.open_bundle`'s; the paths are
:class:`~jasper.active_speaker.crossover_v2.record_store.BankedRecordStore`'s;
the take records are :mod:`~jasper.active_speaker.crossover_v2.spatial`'s four
builders; the banked VERIFY curve is
:func:`~jasper.active_speaker.crossover_v2.durable_state._decimate_verify_measured`'s
output. Nothing below hand-types a record shape, so a writer that changes
fails the suites that read it instead of leaving a fixture agreeing with
nothing that ships.

**The two shapes are DISJOINT, and that is the finding they exist to hold.**
``jasper.web.correction_crossover_v2``'s own words: *"stage 2 opens a new
bundle under a new relay session id"*. So one ``bank-crossover-round.sh`` run
banks ONE stage, and:

* :func:`bank_measure_round` — stage 1. CHECK, the design-axis MEASURE take
  carrying both per-driver solos, one lateral walk pose, and the ENTRY
  BASELINE. No cloud group (``capture_plan.STAGE1_INCLUDES_CLOUD_MEASURE`` is
  ``False``), therefore no ``cloud_verify.json``, therefore no cloud positions
  and no graded ``spec`` block. Its flow state banks no VERIFY curve, because
  ``verify_priors`` is rebuilt from the conductor on every persist and a
  stage-1 conductor has measured no VERIFY.
* :func:`bank_verify_round` — stage 2. The VERIFY take, and a flow state
  carrying ``verify_priors.verify_measured``. No per-driver solos: a verify
  stage walks none.

No round carries both a prediction basis and a measured VERIFY sum, which is
issue #3482's root fact; no round carries both an entry baseline and a graded
spec, which is #3478's.

**The cloud group is deliberately absent from BOTH.** Stage 2 banks one, but
no reader these suites pin opens it, and the only way to build a
``cloud_verify.json`` from its own writer is
``spatial.assemble_cloud_group_result`` over a combiner result built from live
captures. A hand-typed cloud payload here would be the one part of this
fixture that could drift, so the position-graded views keep the payload
builder that already lives with them.

**No WAVs.** ``bank-crossover-round.sh`` stopped pulling the capture-dump ring
when the ring was removed, and no reader on these paths opens one.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.contracts import (
    DRIVER_ROLE_TWEETER,
    DRIVER_ROLE_WOOFER,
    REFERENCE_MARK_DESIGN_AXIS,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    _decimate_verify_measured,
)
from jasper.active_speaker.crossover_v2.journey import (
    LATERAL_CONSUMER_FC_SELECTOR,
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore

from tests.active_speaker_fixtures import mono_output_topology

__all__ = [
    "ENTRY_GRID_HZ",
    "MODE_TWO_WAY",
    "MODE_WAY1",
    "SOLO_BAND_HZ",
    "SOLO_GRID_HZ",
    "VERIFY_GRID_HZ",
    "bank_measure_round",
    "bank_verify_round",
]

#: The two SHAPES ``bank_measure_round`` can bank, spelled as the topology
#: fixture's own group modes so a round's bundle and its solos cannot disagree
#: about how many branches the speaker has.
MODE_TWO_WAY = "active_2_way"
MODE_WAY1 = "full_range_passive"

#: The per-driver solos' grid and each driver's own swept band. One band for
#: both roles keeps ``BranchPair.sum_band_hz`` equal to it, so a pin can state
#: the compared span without re-deriving a union.
SOLO_BAND_HZ = (200.0, 12000.0)
SOLO_GRID_HZ = np.linspace(SOLO_BAND_HZ[0], SOLO_BAND_HZ[1], 256)

#: The entry baseline's own reduced grid — log-spaced across all three
#: ``flat_spec.SPEC_BANDS`` rows so a grade has bins in every band.
ENTRY_GRID_HZ = np.geomspace(280.0, 16000.0, 90)

#: The grid the VERIFY capture's own banked curve sits on. Deliberately NOT
#: the solos' grid: the persisted VERIFY pair is on the capture's own
#: frequencies, and a reader that handed one back for the other would pass a
#: same-grid fixture.
VERIFY_GRID_HZ = np.geomspace(SOLO_BAND_HZ[0], SOLO_BAND_HZ[1], 301)

#: Where the fixture's two synthetic branches cross. A SHAPE knob, not a
#: measured corner: it only has to sit inside :data:`SOLO_BAND_HZ` so the
#: summed prediction has a crossover region inside the compared span.
_CROSSOVER_HZ = 1800.0
_RELAY_SESSION_ID = "relay-1"


def _lr4(freqs_hz: np.ndarray, *, highpass: bool) -> np.ndarray:
    """One LR4 branch: an in-phase aligned pair sums flat through Fc."""
    s = 1j * (np.asarray(freqs_hz, dtype=float) / _CROSSOVER_HZ)
    butter2 = (s**2 if highpass else 1.0) / (s**2 + math.sqrt(2.0) * s + 1.0)
    return butter2**2


def _pose_curves(mode: str) -> tuple[spatial.LateralPoseCurve, ...]:
    """This shape's solos as the curve value both banked shapes carry.

    A 1-way main walks ONE routed solo and declares no corner, so its branch is
    unity across the band rather than half of an LR4 pair.
    """
    branches = (
        (("full_range", np.ones_like(SOLO_GRID_HZ, dtype=complex)),)
        if mode == MODE_WAY1 else (
            (DRIVER_ROLE_WOOFER, _lr4(SOLO_GRID_HZ, highpass=False)),
            (DRIVER_ROLE_TWEETER, _lr4(SOLO_GRID_HZ, highpass=True)),
        )
    )
    return tuple(
        spatial.LateralPoseCurve(
            role=role, freqs_hz=SOLO_GRID_HZ, complex_tf=tf, band_hz=SOLO_BAND_HZ,
        )
        for role, tf in branches
    )


def _solo_curves(mode: str) -> list[dict[str, Any]]:
    """This shape's per-driver solos, through the ONE banked-curve serializer."""
    return [spatial.pose_curve_record(curve) for curve in _pose_curves(mode)]


def _open_round(
    root: Path, name: str, mode: str,
) -> tuple[Path, BankedRecordStore, str]:
    """``<round-dir>/bundle/<session-id>/`` with a real evidence store on it.

    The directory layout is ``bank-crossover-round.sh``'s: the whole session
    bundle untarred under ``bundle/``, with the flow state written beside it
    by the callers below.
    """
    round_dir = Path(root) / name
    info = open_bundle(
        mono_output_topology(mode=mode),
        calibration_id="calibration-test",
        sessions_dir=round_dir / "bundle",
    )
    assert info is not None, "open_bundle refused to open a fixture bundle"
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=str(info["session_id"]),
    )
    return round_dir, BankedRecordStore(
        evidence=store, capture_session_id=_RELAY_SESSION_ID,
    ), str(info["session_id"])


def _bank(store: BankedRecordStore, *records: Mapping[str, Any]) -> None:
    """Every record through the store that writes it, in bank order."""

    async def _run() -> None:
        for record in records:
            await store.bank(record)

    asyncio.run(_run())


def _receipt(round_id: str) -> dict[str, Any]:
    return {
        "kind": ROUND_RECEIPT_KIND,
        "schema_version": 2,
        "round_id": round_id,
        "entry_graph_fingerprint": "fp-entry-graph",
    }


def _state(
    *,
    round_ordinal: int,
    verify_measured: Any,
) -> dict[str, Any]:
    """The flow state ``bank-crossover-round.sh`` drops beside the bundle.

    Narrowed to the keys the readers under test open — the round's place in
    the series, and ``verify_priors.verify_measured``, whose value comes from
    the persist-time writer rather than a hand-typed dict. ``verify_priors``
    is rebuilt from the conductor on every persist, so a ``None`` here is what
    a stage that measured no VERIFY actually banks.
    """
    return {
        "round_receipt": {"round_ordinal": round_ordinal},
        "round_ordinal_epoch": 1,
        "verify_priors": {"verify_measured": verify_measured},
    }


def bank_measure_round(
    root: Path,
    *,
    name: str = "r1-measure",
    entry_baseline_db: np.ndarray | None = None,
    entry_excluded: Sequence[bool] | None = None,
    round_ordinal: int = 1,
    mode: str = MODE_TWO_WAY,
) -> Path:
    """One STAGE-1 round directory, as the flow banks it.

    CHECK, the design-axis MEASURE take carrying both per-driver solos, one
    lateral walk pose, and the entry baseline — plus the round receipt and the
    flow state. No cloud group and therefore no graded ``spec``: this is the
    only round shape that produces an entry baseline, and it is the shape the
    forward model's own worked example points at.

    ``entry_baseline_db`` defaults to a flat -20 dB curve on
    :data:`ENTRY_GRID_HZ`. ``mode`` picks the SHAPE: :data:`MODE_TWO_WAY` walks
    both solos, :data:`MODE_WAY1` the one a subless passive main has.
    """
    round_dir, store, session_id = _open_round(root, name, mode)
    magnitude_db = (
        np.full(ENTRY_GRID_HZ.shape, -20.0)
        if entry_baseline_db is None else np.asarray(entry_baseline_db, dtype=float)
    )
    excluded = (
        [False] * int(ENTRY_GRID_HZ.size)
        if entry_excluded is None else [bool(flag) for flag in entry_excluded]
    )
    stamp = {
        "session_id": session_id,
        "graph_fingerprint": "fp-entry-graph",
        "captured_at": "2026-08-31T22:00:00Z",
        "wav_sha256": "a" * 64,
    }
    _bank(
        store,
        # CHECK computes no transfer function, so it banks an empty curve list
        # — the writer's own shape, not an omission here.
        spatial.phase_capture_record(
            phase=PHASE_CHECK, index=1, attempt=1, curves=(), **stamp,
        ),
        spatial.phase_capture_record(
            phase=PHASE_MEASURE, index=2, attempt=1, curves=_solo_curves(mode),
            **stamp,
        ),
        # One lateral pose, so a reader selecting the MEASURE take has a
        # sibling take of another phase to pass over rather than a clear field.
        spatial.lateral_pose_record(
            spatial.LateralPose(
                pose_id="lateral_03", index=3, attempt=1, prompt="", role="",
                offset_cm=0.0, at_mark=True,
                curves=_pose_curves(mode),
            ),
            position_deg=7, lateral_consumer=LATERAL_CONSUMER_FC_SELECTOR,
            **stamp,
        ),
        spatial.entry_baseline_record(
            index=4, attempt=1,
            program_id="prog-entry", reference_mark=REFERENCE_MARK_DESIGN_AXIS,
            freqs_hz=ENTRY_GRID_HZ, magnitude_db=magnitude_db, excluded=excluded,
            # Capture SCALARS the record carries and every reader in these
            # suites drops (read_entry_baseline_take narrows to the identity
            # and the three arrays). Plausible values so the record is whole,
            # never a number any pin reads.
            validity_floor_hz=200.0, gate_window_ms=7.0, summed_ripple_db=1.0,
            glitch_detected=False, **stamp,
        ),
        _receipt("r1"),
    )
    (round_dir / "state.json").write_text(
        json.dumps(_state(round_ordinal=round_ordinal, verify_measured=None))
    )
    return round_dir


def bank_verify_round(
    root: Path,
    *,
    name: str = "r2-verify",
    measured_db: np.ndarray | None = None,
    round_ordinal: int = 2,
) -> Path:
    """One STAGE-2 round directory, as the flow banks it.

    The VERIFY take, plus a flow state carrying the VERIFY capture's own
    measured curve. It banks NO per-driver solos, so it can supply a
    forward-model comparison's measured half and never its prediction basis.

    ``measured_db`` defaults to a flat -30 dB curve on :data:`VERIFY_GRID_HZ`.
    """
    round_dir, store, session_id = _open_round(root, name, MODE_TWO_WAY)
    measured = (
        np.full(VERIFY_GRID_HZ.shape, -30.0)
        if measured_db is None else np.asarray(measured_db, dtype=float)
    )
    stamp = {
        "session_id": session_id,
        "graph_fingerprint": "fp-applied-graph",
        "captured_at": "2026-08-31T23:00:00Z",
        "wav_sha256": "c" * 64,
    }
    _bank(
        store,
        spatial.phase_capture_record(
            phase=PHASE_VERIFY, index=1, attempt=1,
            curves=[
                spatial.pose_curve_record(
                    spatial.LateralPoseCurve(
                        role="summed", freqs_hz=VERIFY_GRID_HZ,
                        complex_tf=10.0 ** (measured / 20.0),
                        band_hz=SOLO_BAND_HZ,
                    )
                )
            ],
            **stamp,
        ),
        _receipt("r2"),
    )
    (round_dir / "state.json").write_text(json.dumps(_state(
        round_ordinal=round_ordinal,
        verify_measured=_decimate_verify_measured(
            (VERIFY_GRID_HZ, measured, np.zeros_like(measured))
        ),
    )))
    return round_dir
