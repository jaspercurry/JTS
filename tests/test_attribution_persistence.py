# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WO-1 storage contracts: bundle-lifetime retention and per-position evidence.

Pins the acceptance items ``docs/historical/attribution-stage-plan.md`` §7 assigns to
WO-1 that are about *storage* — the Q-C retention model, provenance-marker
discipline, and §6's per-position evidence — plus the flow seam that produces
them. The schema itself is pinned in ``tests/test_attribution_findings.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import shutil
import warnings
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bundles import (
    BUNDLE_FILE_MODE,
    CAPTURE_KIND_SEQUENTIAL,
    enforce_retention,
    open_bundle,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2_flow import (
    assemble_cloud_group_result,
    cloud_position_capture,
)
from jasper.attribution.findings import (
    EVIDENCE_STORE_BUNDLE,
    EvidenceRef,
    Finding,
    FindingSet,
)
from jasper.attribution.position_evidence import (
    CURVE_FRACTIONAL_OCTAVE,
    FIELD_DESCRIPTIONS,
    POSITION_EVIDENCE_SCHEMA,
    log_grid_hz,
    position_evidence_block,
)
from jasper.attribution.promotion import PRODUCED_BY
from jasper.attribution.session_identity import (
    SessionIdentity,
    read_session_identity,
)
from jasper.attribution.storage import (
    FindingEvidenceMissing,
    FindingStorageError,
    bundle_evidence_ref,
    findings_relative_path,
    publish_finding_set,
    read_finding_set,
)
from jasper.audio_measurement.spatial_combine import (
    CombinedResponse,
    EchoDiagnostic,
    PositionCapture,
    combine_positions,
)
from tests import _flat_lin_corpus as corpus
from tests.active_speaker_fixtures import mono_output_topology

CAPTURE = "cap_Ktm3xQ2p"
PHASE = "cloud_measure"


def _open_store(tmp_path: Path) -> tuple[CommissioningEvidenceStore, Path]:
    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    return store, Path(info["bundle_dir"])


def _seed_evidence(store: CommissioningEvidenceStore) -> tuple[SessionIdentity, EvidenceRef]:
    """Publish a stand-in cloud artifact and cite it, as the live seam does."""

    identity = SessionIdentity(session_id=store.session_id)
    artifact = store.publish_json_artifact(
        f"crossover_v2/{CAPTURE}/{PHASE}.json",
        {"schema_version": 1, "kind": "jts_crossover_v2_cloud_evidence"},
    )
    return identity, bundle_evidence_ref(artifact, identity)


def _finding(cite: EvidenceRef) -> Finding:
    return Finding(
        mechanism="M2",
        band_hz=(4200.0, 4600.0),
        evidence={"tau_us": 310.0},
        confidence="unsure",
        fix_class="carve",
        household_copy=(
            "A narrow range here cancels rather than plays quietly, and "
            "adding level cannot fill a cancellation."
        ),
        probes_run=("P2",),
        probes_recommended=("P4",),
        cites=(cite,),
    )


# --------------------------------------------------------------------------- #
# Q-C — bundle-lifetime retention (§7 acceptance)
# --------------------------------------------------------------------------- #


def test_a_finding_round_trips_through_the_real_evidence_store(tmp_path: Path) -> None:
    store, _ = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    built = FindingSet(
        session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
    )

    publish_finding_set(
        store, capture_session_id=CAPTURE, phase=PHASE, finding_set=built
    )
    reopened = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)

    assert reopened == built


def test_a_finding_does_not_outlive_the_bundle_that_holds_its_evidence(
    tmp_path: Path,
) -> None:
    """Q-C, ruled by the owner 2026-07-29 night: "A finding lives inside the
    session bundle whose evidence it cites and dies with it."

    Retention evicts a bundle with ``shutil.rmtree`` — the whole directory,
    findings and evidence together — so this half needs no retention code at
    all. That is exactly why it is pinned: a guarantee that holds only
    because of the storage shape is the kind that quietly stops holding when
    someone adds a per-file pruner.
    """

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    findings_path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(
        CAPTURE, PHASE
    )
    assert findings_path.is_file()

    shutil.rmtree(bundle_dir)

    assert not findings_path.exists()
    # And the evidence it cited is gone in the same act — neither can survive
    # the other.
    assert not (bundle_dir / cite.locator).exists()


def test_bundle_eviction_takes_findings_and_evidence_together(
    tmp_path: Path,
) -> None:
    """The same rule through the real retention path rather than a hand
    ``rmtree``: ``enforce_retention`` is a two-axis ring (count and bytes) and
    deletes whole bundles, so there is no tier that could keep a finding
    whose evidence was pruned."""

    sessions = tmp_path / "sessions"
    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    # A newer bundle, so the one under test is no longer retention-protected.
    open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=sessions,
    )

    enforce_retention(sessions, max_bundles=1, max_bytes=10**9)

    assert not bundle_dir.exists()


def test_a_finding_may_not_silently_predecease_its_evidence(
    tmp_path: Path,
) -> None:
    """The loud half of the same rule. Nothing prunes *inside* a living
    bundle today, but a finding that cited a vanished artifact would be the
    more dangerous failure — a diagnosis that still reads fine and rests on
    nothing. Reading must say so, not return the finding."""

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )

    (bundle_dir / cite.locator).unlink()

    with pytest.raises(FindingEvidenceMissing, match="no longer produce"):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    # An explicit opt-out still exists for a caller that wants the record
    # without its support — but it has to ask.
    assert read_finding_set(
        store, capture_session_id=CAPTURE, phase=PHASE, verify_evidence=False
    ) is not None


def test_altered_evidence_is_as_loud_as_missing_evidence(tmp_path: Path) -> None:
    """The digest is the verifier. Evidence that is present but no longer the
    bytes the finding cited must not read as support."""

    store, _ = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    tampered = EvidenceRef(
        session=identity,
        store=EVIDENCE_STORE_BUNDLE,
        locator=cite.locator,
        sha256="b" * 64,
    )
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity,
            produced_by=PRODUCED_BY,
            findings=(_finding(tampered),),
        ),
    )

    with pytest.raises(FindingEvidenceMissing, match="but the bundle now holds"):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)


# --------------------------------------------------------------------------- #
# Provenance marker discipline (§7 acceptance)
# --------------------------------------------------------------------------- #


def test_a_bundle_written_before_attribution_stays_readable(tmp_path: Path) -> None:
    """§7 acceptance: "provenance marker discipline (old bundles without
    findings readable)". Absence is a state, not an error."""

    store, _ = _open_store(tmp_path)
    _seed_evidence(store)

    assert read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE) is None


def test_ran_and_found_nothing_is_not_the_same_as_nobody_looked(
    tmp_path: Path,
) -> None:
    """The whole point of the marker. An empty ``findings`` list inside a
    present record says the speaker is clean; an absent record says nothing
    was ever run. Conflating them would make "no findings" unreadable."""

    store, _ = _open_store(tmp_path)
    identity, _cite = _seed_evidence(store)

    assert read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE) is None
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=()
        ),
    )
    empty = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)

    assert empty is not None
    assert empty.findings == ()
    assert empty.produced_by == PRODUCED_BY


def test_a_corrupt_record_is_a_failure_not_a_legacy_bundle(tmp_path: Path) -> None:
    """An unreadable artifact must never be mistaken for "this bundle
    predates attribution" — that would turn a real fault into a shrug."""

    store, bundle_dir = _open_store(tmp_path)
    path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(CAPTURE, PHASE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"jts_attribution_finding_set/1"}', encoding="utf-8")

    with pytest.raises(FindingStorageError):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)


def test_findings_inherit_the_bundle_s_group_readable_mode(tmp_path: Path) -> None:
    """§6's ownership posture: "no **root-owned** artifacts on a laptop-pull
    path … Group-readable ownership plus the existing modes is the shape."
    Findings are published through the same store as every other artifact, so
    they inherit ``BUNDLE_FILE_MODE`` and the parent's group rather than
    inventing a posture of their own."""

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    publish_finding_set(
        store,
        capture_session_id=CAPTURE,
        phase=PHASE,
        finding_set=FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(CAPTURE, PHASE)

    assert BUNDLE_FILE_MODE == 0o640
    assert path.stat().st_mode & 0o777 == BUNDLE_FILE_MODE
    assert path.stat().st_gid == path.parent.stat().st_gid


# --------------------------------------------------------------------------- #
# Per-position evidence (§6, §11.1 A7, §11.2 G9's data half)
# --------------------------------------------------------------------------- #


def _echo(tau_us: float) -> EchoDiagnostic:
    return EchoDiagnostic(
        tau_us=tau_us,
        strength_db=-8.6,
        confidence=0.7,
        refusal="",
        resolution_us=2.6,
        tau_cepstral_us=tau_us,
        tau_envelope_us=tau_us,
        concentration=0.62,
        corroboration=0.91,
        arrival_crest_db=12.0,
    )


def _combined(n: int = 3, *, with_echo: bool = True) -> CombinedResponse:
    """A REAL ``CombinedResponse``, built through the shipped combiner.

    Deliberately not a stand-in object: the whole WO-1 claim is that the
    per-position members are already sitting on this type at group close, so
    a test double carrying only the fields the serializer happens to read
    would prove nothing about that.
    """

    bins, rate = 8192, 48_000
    freqs = np.fft.rfftfreq(2 * (bins - 1), 1 / rate)
    combined = combine_positions(
        [
            PositionCapture(
                position_id=f"cloud_measure_{i:02d}",
                freqs_hz=freqs,
                magnitude_db=np.full(freqs.size, -30.0 - i),
                sample_rate=rate,
            )
            for i in range(n)
        ]
    )
    if not with_echo:
        return combined
    # Synthetic positions carry no impulse response, so the combiner reports
    # no echo diagnostic for them. Inject real ones so the scalars §6 names
    # have something to survive.
    return dataclasses.replace(
        combined, per_position_echo=tuple(_echo(300.0 + i) for i in range(n))
    )


def _records(n: int = 3) -> list[dict]:
    return [
        {
            "position_id": f"cloud_measure_{i:02d}",
            "index": i,
            "attempt": 2 if i == 1 else 1,
            "take_id": f"cloud_measure_{i:02d}_a{2 if i == 1 else 1:02d}",
            "role": ("onax", "offax", "xovr")[i % 3],
            "gate_window_ms": 7.0,
            # The 2026-07-30 corpus's own state: the window is the SEARCH
            # CEILING, not a found reflection (#1966).
            "gate_floor_source": "search_span_bound",
            "gate_disclosure": (
                "no reflection found; window capped at the 7.00 ms search "
                "ceiling, so nothing was gated out, valid above 143 Hz and "
                "trusted above 357 Hz"
            ),
            "validity_floor_hz": 142.9,
            "gating_applied": True,
            "summed_ripple_db": 3.1,
            "glitch_detected": False,
            "wav_sha256": f"{i:064d}",
        }
        for i in range(n)
    ]


def test_the_members_survive_alongside_the_aggregate() -> None:
    """§11.1 A7, WO-1's half: "Plot the members alongside the aggregate;
    never ship the aggregate alone." Four schools state it and the Q-D
    bake-off's clearest leg depends on having members. Before this, the
    combiner computed every member curve and the persist step dropped them."""

    block = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )

    assert block["available"] is True
    assert block["schema"] == POSITION_EVIDENCE_SCHEMA
    assert len(block["positions"]) == 3
    for row in block["positions"]:
        assert row["magnitude_db"]
        assert len(row["magnitude_db"]) == len(block["curve_grid"]["freqs_hz"])


def test_each_position_carries_the_named_question_it_answers() -> None:
    """The attribution stage's actual consumer is this block, not the bundle
    sidecar — so a role that reaches only the sidecar reaches nobody.

    ``role`` (attribution plan §5's promotion-queue item 1, shipped with T4's
    position prompts) is projected through ``_RECORD_FIELDS`` like every other
    per-position scalar, and it is READ off the record rather than re-derived
    from the prompt string: the prompt is household copy that product rulings
    rewrite, which is the whole reason the label exists as data.
    """
    block = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )

    assert [row["role"] for row in block["positions"]] == ["onax", "offax", "xovr"]
    # …and the block explains the vocabulary to whoever reads the artifact,
    # including that an absent role is unsampled rather than absent evidence.
    description = block["field_descriptions"]["role"]
    assert "onax" in description and "offax" in description and "xovr" in description
    assert "UNSAMPLED" in description

    # A record that predates the label simply carries no role, rather than one
    # invented from its prompt — the same absence discipline every other
    # optional scalar in this block already follows.
    older = [{k: v for k, v in r.items() if k != "role"} for r in _records()]
    legacy = position_evidence_block(
        _combined(), position_records=older, validity_floor_hz=142.9
    )
    assert all("role" not in row for row in legacy["positions"])


def test_the_per_position_curve_is_at_least_a_twelfth_octave_from_the_floor() -> None:
    """§6's stated shape: "the per-position analysed curve (>= 1/12-octave
    from the validity floor up)". The grid starts at the group's own gated
    validity floor, not at an assumed one."""

    block = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )
    grid = block["curve_grid"]

    assert grid["fractional_octave"] == CURVE_FRACTIONAL_OCTAVE == 12
    assert grid["floor_source"] == "validity_floor_hz"
    assert grid["floor_hz"] == pytest.approx(142.9, abs=0.01)
    freqs = grid["freqs_hz"]
    assert freqs[0] == pytest.approx(142.9, abs=0.01)
    # Consecutive points are one twelfth-octave apart, so the grid genuinely
    # resolves that finely rather than merely claiming to.
    ratios = [freqs[i + 1] / freqs[i] for i in range(len(freqs) - 1)]
    assert all(r == pytest.approx(2 ** (1 / 12), rel=1e-6) for r in ratios)


def test_the_grid_declares_its_real_resolution_not_just_its_density() -> None:
    """§6: "estimator grid resolution is declared in the artifact". The grid
    is 1/12 octave; the curve sampled onto it is already smoothed at the
    combiner's diagnostic fraction, so the honest resolution is that
    fraction. Reporting only the grid would overstate it."""

    grid = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["curve_grid"]

    assert grid["smoothing_fraction"] == 6
    assert grid["fractional_octave"] == 12


def test_a_missing_validity_floor_is_named_not_invented() -> None:
    block = position_evidence_block(_combined(), position_records=_records())

    assert block["curve_grid"]["floor_source"] == "default_floor"
    assert block["curve_grid"]["floor_hz"] == pytest.approx(20.0)


def test_the_per_position_scalars_the_pipeline_already_had_survive() -> None:
    """§6 names four: "tau, confidence/prominence, the gate actually used,
    ripple". The gate and the ripple were already on the capture's own
    sidecar; tau and its confidence were computed by the combiner and
    dropped."""

    rows = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["positions"]

    first = rows[0]
    assert first["echo"]["tau_us"] == 300.0
    assert first["echo"]["confidence"] == 0.7
    assert first["echo"]["concentration"] == 0.62
    assert first["echo"]["refusal"] == ""
    assert first["gate_window_ms"] == 7.0
    assert first["summed_ripple_db"] == 3.1


def test_each_position_says_why_its_gate_window_is_what_it_is() -> None:
    """#1966 — ``gate_window_ms`` is projected through ``_RECORD_FIELDS``, and
    so must its provenance be, or the allowlist silently drops it.

    A 7.0 ms window means "the window stops at a measured reflection" or "the
    search reached its ceiling and found nothing" — opposite epistemic states
    that print identically. Every position of the 2026-07-30 corpus was the
    second one, and ``gating_applied: True`` beside it read as the first.
    """
    rows = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["positions"]

    assert all(r["gate_floor_source"] == "search_span_bound" for r in rows)
    # It is NOT the grid's own floor_source, which uses a different vocabulary
    # for a different question and lives one level up.
    grid_source = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["curve_grid"]["floor_source"]
    assert grid_source == "validity_floor_hz"
    assert grid_source != rows[0]["gate_floor_source"]
    # Both fields are described where a reader will look for them.
    assert "gate_floor_source" in FIELD_DESCRIPTIONS


def test_each_position_carries_the_gate_provenance_as_a_sentence_too() -> None:
    """#1966 at the surface: the enum fixed the record for a machine.

    A person opening this file should not have to know that
    ``search_span_bound`` means "nothing was gated out" — the allowlist
    therefore projects the rendered sentence beside the enum, and it must
    say so in words.
    """
    block = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )
    rows = block["positions"]
    assert all("nothing was gated out" in r["gate_disclosure"] for r in rows)
    assert all("reflection measured" not in r["gate_disclosure"] for r in rows)
    assert "gate_disclosure" in FIELD_DESCRIPTIONS
    assert "gate_disclosure" in block["field_descriptions"]


def test_the_numbers_behind_that_sentence_are_projected_and_described() -> None:
    """Ticket 1.5 — the same allowlist argument as the sentence's, one level on.

    The sentence fixed the record for a person; a reader mining the round for
    NUMBERS still had to parse English. ``_RECORD_FIELDS`` therefore projects
    the two figures beside it, and ``FIELD_DESCRIPTIONS`` has to describe them
    or the file grows a column nothing explains.

    The other half is the allowlist's own ``is not None`` rule, which is what
    makes the ceiling-capped row below carry a movement and no delay: an
    absent key here means "this capture had no such number", never "nobody
    banks one" — the distinction ``evidence_packet._gate_numbers_reason``
    depends on.
    """
    records = _records()
    records[0]["gate_moved_rms_db"] = 2.59
    records[0]["gate_reflection_delay_ms"] = 5.33
    # The 2026-07-30 corpus's own state: the window was capped, so the gate
    # still MOVED the response and there was no reflection to time.
    records[1]["gate_moved_rms_db"] = 1.37
    records[1]["gate_reflection_delay_ms"] = None

    block = position_evidence_block(
        _combined(), position_records=records, validity_floor_hz=142.9
    )
    by_id = {row["position_id"]: row for row in block["positions"]}

    assert by_id["cloud_measure_00"]["gate_moved_rms_db"] == 2.59
    assert by_id["cloud_measure_00"]["gate_reflection_delay_ms"] == 5.33
    assert by_id["cloud_measure_01"]["gate_moved_rms_db"] == 1.37
    assert "gate_reflection_delay_ms" not in by_id["cloud_measure_01"]
    # A row whose record carried neither stays as it was — the projection adds
    # nothing it was not given.
    assert "gate_moved_rms_db" not in by_id["cloud_measure_02"]

    for column in ("gate_moved_rms_db", "gate_reflection_delay_ms"):
        assert column in FIELD_DESCRIPTIONS
        assert column in block["field_descriptions"]
    # And the description says the thing a reader gets wrong: the delay is not
    # the gating block's absolute ``first_reflection_ms``.
    assert "first_reflection_ms" in FIELD_DESCRIPTIONS["gate_reflection_delay_ms"]


def test_the_accepted_attempt_is_recoverable_per_position() -> None:
    """§6: "the accepted-attempt <-> position mapping (today retries are
    visible only as skipped attempt indices in filenames)". A geometry retake
    reuses the position id, so the id alone does not name a take."""

    rows = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["positions"]
    by_id = {row["position_id"]: row for row in rows}

    assert by_id["cloud_measure_01"]["attempt"] == 2
    assert by_id["cloud_measure_01"]["take_id"] == "cloud_measure_01_a02"
    assert by_id["cloud_measure_00"]["attempt"] == 1


def test_each_position_carries_its_capture_digest() -> None:
    """§6: "the per-position WAV path + SHA-256 in the state itself so the
    state alone is replayable"."""

    rows = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["positions"]

    assert all(len(row["wav_sha256"]) == 64 for row in rows)


def test_the_block_carries_the_data_the_offax_membership_gate_needs() -> None:
    """§11.2 G9, WO-1's data half (owner row "WO-4 / WO-1"). The two-sided
    6 dB OFFAX membership gate — and G8's equidistance precondition beside it
    — are WO-4's to implement and route. Their INPUT is per-position level and
    per-position delay, which is exactly what a member curve plus its echo
    scalars carry. Before this block existed the data those gates need was
    computed and discarded, so the gates were unimplementable at any cost."""

    rows = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )["positions"]

    # A per-position level difference is computable from the members alone.
    levels = [sum(row["magnitude_db"]) / len(row["magnitude_db"]) for row in rows]
    assert max(levels) - min(levels) == pytest.approx(2.0, abs=0.01)
    # And a per-position delay is present for the equidistance comparison.
    assert all(row["echo"]["tau_us"] > 0 for row in rows)


def test_serialization_never_fails_a_group_it_cannot_describe() -> None:
    """This rides the same diagnostic/disclosure path as the rest of the
    cloud result, where a failure must degrade to an honest "unavailable"
    rather than fail a capture the household already gave."""

    assert position_evidence_block(None)["available"] is False
    assert position_evidence_block(None)["reason"] == "combine_failed"

    class _Empty:
        freqs_hz = np.array([])
        per_position_diag_db = np.empty((0, 0))
        position_ids = ()
        per_position_echo = ()

    assert position_evidence_block(_Empty())["reason"] == "no_per_position_curves"
    assert position_evidence_block(object())["available"] is False


def test_the_block_is_json_native() -> None:
    """The host persists this verbatim into the durable v2 state and into a
    canonical-JSON bundle artifact, so a numpy scalar here is a
    serialization bug."""

    block = position_evidence_block(
        _combined(), position_records=_records(), validity_floor_hz=142.9
    )
    for row in block["positions"]:
        for value in row["magnitude_db"]:
            assert type(value) is float and math.isfinite(value)
        for value in row["echo"].values():
            assert isinstance(value, (str, int, float))
    for value in block["curve_grid"]["freqs_hz"]:
        assert type(value) is float


def test_sampling_emits_no_divide_by_zero_warning() -> None:
    """The ``log2(0)`` guard's real and only observable effect.

    The source grid is an ``rfftfreq`` grid whose first bin is DC. Measured
    (not assumed): dropping it does **not** change a single sampled value,
    because the grid starts at ``max(floor, lowest positive bin)`` and no
    query point can reach the ``[-inf, log2(f1)]`` segment. What it does
    change is that ``np.log2`` no longer warns and the result no longer
    depends on how ``np.interp`` treats a ``-inf`` left edge, which numpy
    does not specify.

    So this pins the guard by the one thing it actually does. Delete the
    ``freqs > 0.0`` filter in ``_sample_onto`` and this test fails; an
    output-equality test would not, which is why the docstring there claims
    only what is true.
    """

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        block = position_evidence_block(
            _combined(), position_records=_records(), validity_floor_hz=142.9
        )

    # And the block really was produced, not short-circuited into an
    # "unavailable" that would pass this vacuously.
    assert block["available"] is True
    assert block["positions"][0]["magnitude_db"]


def test_log_grid_refuses_a_degenerate_range() -> None:
    with pytest.raises(ValueError):
        log_grid_hz(0.0, 20000.0)
    with pytest.raises(ValueError):
        log_grid_hz(1000.0, 1000.0)


# --------------------------------------------------------------------------- #
# The flow seam
# --------------------------------------------------------------------------- #


def test_the_cloud_group_result_carries_its_members(tmp_path: Path) -> None:
    """The persist site: ``assemble_cloud_group_result`` is what lands in the
    durable state AND in the bundle artifact, so this is where the members
    have to appear for §6 to be satisfied."""

    result = assemble_cloud_group_result(
        _combined(),
        echo_band_hz=(4000.0, 16000.0),
        validity_floor_hz=142.9,
        position_records=_records(),
    )

    assert result["available"] is True
    assert result["positions"]["available"] is True
    assert len(result["positions"]["positions"]) == 3
    # The aggregate is unchanged — the members ride alongside it, not instead.
    assert result["curve"]["freqs_hz"]


def test_a_caller_that_passes_no_records_still_gets_the_curves() -> None:
    """``position_records`` is optional so every existing caller and test
    keeps working; the member curves and echo scalars do not depend on it."""

    result = assemble_cloud_group_result(
        _combined(), echo_band_hz=(4000.0, 16000.0)
    )

    rows = result["positions"]["positions"]
    assert len(rows) == 3
    assert all(row["magnitude_db"] for row in rows)
    assert "attempt" not in rows[0]


def test_state_projection_does_not_inherit_the_per_position_block() -> None:
    """``compact_cloud_status`` is a shape-scoped projection: a consumer that
    reads ``cloud`` alone (the doctor) must never have to parse or skip
    curve-shaped data. Adding members to the pipeline result must not leak
    into it."""

    from jasper.active_speaker.crossover_envelope_v2 import compact_cloud_status

    result = assemble_cloud_group_result(
        _combined(),
        echo_band_hz=(4000.0, 16000.0),
        validity_floor_hz=142.9,
        position_records=_records(),
    )
    compact = compact_cloud_status({PHASE: {"pipeline": result, "geometry": {}}})

    assert compact is not None
    assert "positions" not in (compact.get(PHASE) or {})


# --------------------------------------------------------------------------- #
# The cross-store identity, through the real web seams (§6, §7 acceptance)
# --------------------------------------------------------------------------- #


def _carve_outs() -> list[dict]:
    return [
        {
            "band_hz": [2000.0, 8000.0],
            "intervals": [
                {
                    "f_lo_hz": 4200.0,
                    "f_hi_hz": 4600.0,
                    "source": "identified_null",
                    "f_center_hz": 4400.0,
                    "n": 3,
                    "tau_us": 310.0,
                    "r_time": 0.28,
                    "r_freq": 0.31,
                    "depth_db": -6.4,
                    "classification": "position_invariant",
                    "reason": (
                        "This range is a cancellation between two arrivals. "
                        "Adding level cannot fill a cancellation, so it is "
                        "left out of correction and out of grading."
                    ),
                }
            ],
        }
    ]


def test_the_cloud_artifact_and_its_findings_share_one_identity(
    tmp_path: Path,
) -> None:
    """The hop that matters most: the finding set and the evidence it cites
    resolve to the same session, and the capture id rides as an alias rather
    than as a competing identity."""

    from jasper.web import correction_crossover_v2 as v2host

    store, bundle_dir = _open_store(tmp_path)
    refs: dict = {}
    v2host.bind_cloud_publisher(store, CAPTURE, refs)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )

    cloud = json.loads(
        (
            bundle_dir
            / "evidence/v1/artifacts/crossover_v2"
            / CAPTURE
            / f"{PHASE}.json"
        ).read_text()
    )
    cloud_identity = read_session_identity(cloud)
    assert cloud_identity is not None
    assert cloud_identity.session_id == store.session_id
    assert cloud_identity.aliases["capture_session_id"] == CAPTURE

    findings = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert findings is not None
    assert findings.session == cloud_identity


def test_the_live_seam_promotes_carve_outs_and_cites_the_cloud_artifact(
    tmp_path: Path,
) -> None:
    """End to end through the real publisher: the excluded-band records become
    findings, and each cites the exact cloud artifact its numbers were read
    from — so ``read_finding_set``'s default verification has something real
    to check."""

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2host.bind_cloud_publisher(store, CAPTURE, refs)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )

    findings = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert findings is not None
    assert [f.mechanism for f in findings.findings] == ["M2"]
    assert findings.findings[0].confidence == "unsure"
    assert findings.findings[0].fix_class == "carve"
    assert refs["finding_artifacts"][PHASE]
    cite = findings.findings[0].cites[0]
    assert cite.locator.endswith(f"crossover_v2/{CAPTURE}/{PHASE}.json")
    assert len(cite.sha256) == 64


def test_a_findings_failure_never_fails_the_cloud_publish(tmp_path: Path) -> None:
    """§3.4: findings are *optional* evidence artifacts — "a session with no
    findings behaves exactly as it does today". So this seam fails soft,
    unlike its two strict siblings, and the cloud artifact it rides behind is
    already durable by the time findings run."""

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}

    # A carve-out block shaped in a way promotion cannot read at all.
    v2host.bind_cloud_publisher(store, CAPTURE, refs)(
        PHASE, {"available": True, "carve_outs": "not-a-list"}
    )

    assert refs["cloud_artifacts"][PHASE]
    empty = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert empty is not None and empty.findings == ()


# --------------------------------------------------------------------------- #
# The level-frame finding's own publish seam (#1866, 2026-07-30)
# --------------------------------------------------------------------------- #

_LEVEL_FRAME_RECORD = {
    "f_lo_hz": 150.0,
    "f_hi_hz": 5844.7,
    "disagreement_db": 3.209,
    "tolerance_db": 3.0,
    "reference_role": "woofer",
    "system_level_db": 3.209,
    "realized_difference_db": -0.828,
    "realized_tolerance_db": 3.0,
    "realized_level_w_db": 0.213,
    "realized_level_t_db": -0.615,
    "core_level_db_woofer": 3.276,
    "core_band_lo_hz_woofer": 150.0,
    "core_band_hi_hz_woofer": 1255.8,
    "radiating_band_lo_hz_woofer": 0.0,
    "radiating_band_hi_hz_woofer": 1282.3,
    "trim_band_average_db_woofer": -0.067,
    "core_level_db_tweeter": 0.0,
    "core_band_lo_hz_tweeter": 2020.0,
    "core_band_hi_hz_tweeter": 5844.7,
    "radiating_band_lo_hz_tweeter": 1996.4,
    "radiating_band_hi_hz_tweeter": None,
    "trim_band_average_db_tweeter": 0.0,
}


def _publish_candidate_artifact(store: CommissioningEvidenceStore) -> None:
    """The artifact the level-frame finding cites, as the real seam leaves it.

    The finding is minted immediately after ``publish_candidate`` in
    ``_commit_measure_candidate``, so a test of the findings seam has to stand
    the same thing up: a citation into a bundle that does not hold the
    artifact is exactly the failure ``read_finding_set`` exists to catch.
    """
    store.publish_json_artifact(
        f"crossover_v2/{CAPTURE}/candidate.json",
        {"kind": "jts_crossover_v2_candidate", "linearization": {}},
    )


def test_the_banked_frame_finding_lands_in_the_bundle_and_reopens(
    tmp_path: Path,
) -> None:
    """The ruling's persistence half, end to end through the real seam: the
    conductor's banked record becomes a durable M7 finding that reopens with
    its evidence verified.

    ``read_finding_set`` re-resolves and re-hashes every bundle citation by
    default, so a successful read here is also the proof that the finding
    cites something the bundle can still produce — the property that makes a
    diagnosis checkable rather than merely believable.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    _publish_candidate_artifact(store)
    v2host.bind_findings_publisher(store, CAPTURE, refs)(_LEVEL_FRAME_RECORD)

    findings = read_finding_set(store, capture_session_id=CAPTURE, phase="measure")
    assert findings is not None
    assert [f.mechanism for f in findings.findings] == ["M7"]
    finding = findings.findings[0]
    assert finding.fix_class == "document_as_physics"
    assert finding.confidence == "unsure"
    assert finding.band_hz == (150.0, 5844.7)
    # All three level instruments survived the hop.
    assert finding.evidence["core_level_db_woofer"] == 3.276
    assert finding.evidence["trim_band_average_db_woofer"] == -0.067
    assert finding.evidence["realized_difference_db"] == -0.828
    # …and the citation is the candidate this finding is about.
    cite = finding.cites[0]
    assert cite.locator.endswith(f"crossover_v2/{CAPTURE}/candidate.json")
    assert len(cite.sha256) == 64
    assert refs["finding_artifacts"]["measure"]


def test_the_frame_finding_takes_its_own_phase_so_the_cloud_set_survives(
    tmp_path: Path,
) -> None:
    """Its own phase is FORCED, not chosen.

    The store is write-once and the cloud group's finding set is published at
    group CLOSE — seconds and one household tap before the fit this finding
    comes out of even runs. Sharing a phase would be a ``PATH_CONFLICT`` on the
    second write, not a merge. This pins that both sets exist afterwards and
    neither overwrote the other.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2host.bind_cloud_publisher(store, CAPTURE, refs)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )
    _publish_candidate_artifact(store)
    v2host.bind_findings_publisher(store, CAPTURE, refs)(_LEVEL_FRAME_RECORD)

    cloud_set = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    frame_set = read_finding_set(store, capture_session_id=CAPTURE, phase="measure")
    assert cloud_set is not None and frame_set is not None
    assert [f.mechanism for f in cloud_set.findings] == ["M2"]
    assert [f.mechanism for f in frame_set.findings] == ["M7"]
    # One session, two sets, and the same identity on both — which is what
    # lets a reader join them.
    assert cloud_set.session == frame_set.session
    assert cloud_set.produced_by != frame_set.produced_by


def test_a_frame_finding_failure_never_costs_the_session(tmp_path: Path) -> None:
    """§3.4 again, at the seam this time: a findings failure must not raise
    into a conductor that has already published a candidate and already ruled
    the session may proceed.

    Two shapes. **The citation cannot be resolved** — the candidate artifact
    is missing, which is a genuine store failure — and **the record is
    unpromotable**. Neither raises, and neither writes an empty set: "attribution
    ran and found nothing" is a different, false statement about a session whose
    gate found something and said so in the journal.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    # (a) no candidate artifact to cite.
    v2host.bind_findings_publisher(store, CAPTURE, refs)(_LEVEL_FRAME_RECORD)
    assert read_finding_set(store, capture_session_id=CAPTURE, phase="measure") is None
    assert "finding_artifacts" not in refs

    # (b) a record promotion cannot read.
    _publish_candidate_artifact(store)
    v2host.bind_findings_publisher(store, CAPTURE, refs)(
        {**_LEVEL_FRAME_RECORD, "f_hi_hz": None}
    )
    assert read_finding_set(store, capture_session_id=CAPTURE, phase="measure") is None
    assert "finding_artifacts" not in refs


def test_the_banked_finding_is_read_back_for_the_household_to_see(
    tmp_path: Path,
) -> None:
    """WO-1's READ half at the seam (panel lens C, CC1).

    Before this, ``read_finding_set`` had zero non-test callers: the flow banked
    a finding with validated household copy and nothing ever read one back, so
    #1949's "bank a finding and proceed" reduced, in the household's
    experience, to "proceed". The publisher now reopens what it just wrote —
    through the STRICT reader, citation re-hash and all — and projects the one
    field a household may read into the durable refs the screens render from.
    """

    from jasper.attribution.promotion import LEVEL_FRAME_HOUSEHOLD_COPY
    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    _publish_candidate_artifact(store)
    v2host.bind_findings_publisher(store, CAPTURE, refs)(_LEVEL_FRAME_RECORD)

    projected = refs[v2host.FINDING_HOUSEHOLD_REFS_KEY]
    assert len(projected) == 1
    # The sentence is the producer's, copied, never re-minted here.
    assert projected[0]["household_copy"] == LEVEL_FRAME_HOUSEHOLD_COPY
    assert projected[0]["at"] > 0
    # ``household_copy`` and a clock — nothing else. The mechanism id, the
    # evidence scalars, the confidence tier, the probe lists and the citations
    # are INTERNAL taxonomy (findings.py's two-vocabularies rule) and must not
    # be on a household wire even unrendered, where a future renderer could
    # reach them by accident.
    assert set(projected[0]) == {"household_copy", "at"}
    wire = json.dumps(projected)
    for internal in ("M7", "document_as_physics", "unsure",
                     "core_level_db_woofer", "3.276"):
        assert internal not in wire, f"{internal} crossed onto the household wire"


def test_a_carve_out_set_is_recorded_but_never_projected(tmp_path: Path) -> None:
    """The deliberate boundary: the store holds every finding; the household
    wire holds only sentences no other surface already owns.

    A carve-out finding's ``household_copy`` is COPIED from the carve-out record
    (``promote_carve_outs`` rule 3), so ``carve_outs_by_band`` already owns that
    copy and already renders the fact on both those screens: its ``disclosure``
    register is the chart callout's plain-language headline (``cloud.js``'s
    ``buildCallout``) and its ``expert`` register is the τ/r line
    ``_carve_out_expert_lines`` folds into ``expert_details``. Projecting it
    again would put one fact on one screen twice, from two owners — the
    single-source-of-truth failure this program is most careful about. Pinned so
    a future edit that "completes" the projection has to argue with this test
    rather than quietly duplicate the copy.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2host.bind_cloud_publisher(store, CAPTURE, refs)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )
    # Recorded — the durable finding set exists and reopens.
    banked = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert banked is not None and [f.mechanism for f in banked.findings] == ["M2"]
    # …and NOT on the household wire.
    assert v2host.FINDING_HOUSEHOLD_REFS_KEY not in refs


def test_a_finding_whose_evidence_vanished_is_never_projected(
    tmp_path: Path,
) -> None:
    """Rehydration honesty: the read-back is what stands between a household
    and a diagnosis that reads fine and rests on nothing.

    ``read_finding_set`` re-resolves and re-hashes every bundle citation and
    raises ``FindingEvidenceMissing`` when it cannot confirm one. The projection
    must let that stop it — rendering the sentence anyway would be exactly the
    failure ``verify_finding_evidence`` exists to prevent, moved one layer out
    to where a household would read it. Nothing is projected, nothing is
    invented, and the session is not harmed (§3.4).
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, bundle = _open_store(tmp_path)
    refs: dict = {}
    _publish_candidate_artifact(store)
    v2host.bind_findings_publisher(store, CAPTURE, refs)(_LEVEL_FRAME_RECORD)
    assert refs[v2host.FINDING_HOUSEHOLD_REFS_KEY]

    # The cited artifact goes away underneath a second read.
    (
        Path(store.bundle_dir)
        / "evidence" / "v1" / "artifacts" / "crossover_v2" / CAPTURE / "candidate.json"
    ).unlink()
    with pytest.raises(FindingEvidenceMissing):
        read_finding_set(store, capture_session_id=CAPTURE, phase="measure")

    fresh: dict = {}
    v2host._bank_household_findings(
        store, capture_session_id=CAPTURE, phase="measure", refs=fresh,
    )
    assert fresh == {}
    assert bundle.exists()


def test_a_missing_finding_set_projects_nothing_rather_than_an_empty_claim(
    tmp_path: Path,
) -> None:
    """Absence stays absence. A bundle with no set for this phase is the
    legacy/never-banked state — ``read_finding_set`` answers ``None`` — and the
    projection must add no row for it. An empty list here would be
    indistinguishable from "attribution ran and found nothing", which is a
    claim this session never made.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2host._bank_household_findings(
        store, capture_session_id=CAPTURE, phase="measure", refs=refs,
    )
    assert refs == {}


def test_position_retention_puts_the_wav_path_and_digest_in_the_state(
    tmp_path: Path,
) -> None:
    """§6: "the per-position WAV path + SHA-256 **in the state itself** so the
    state alone is replayable", and the accepted-attempt mapping alongside it.
    ``refs`` is what the durable v2 state persists as its evidence block."""

    from jasper.web import correction_crossover_v2 as v2host

    store, bundle_dir = _open_store(tmp_path)
    refs: dict = {}
    bank = v2host.bind_position_retention(store, CAPTURE, refs, asyncio.run)

    class _Result:
        wav = b"take-bytes"

    bank(
        _Result(),
        {
            "position_id": "cloud_measure_03",
            "phase": PHASE,
            "index": 3,
            "attempt": 2,
            "take_id": "cloud_measure_03_a02",
            "measure_kind": "",
            "prompt": "Two hand-widths LEFT of the mark.",
            "wide": False,
            "captured_at": 1.0,
            "session_id": CAPTURE,
            "wav_sha256": "c" * 64,
        },
    )

    entry = refs["position_artifacts"][0]
    assert entry["take_id"] == "cloud_measure_03_a02"
    assert entry["attempt"] == 2
    assert entry["wav_sha256"] == "c" * 64
    assert entry["wav_path"]
    # The path is bundle-relative and really points at the retained bytes.
    assert (bundle_dir / entry["wav_path"]).read_bytes() == b"take-bytes"


@pytest.mark.parametrize(
    ("phase", "expected_kind"),
    [
        (PHASE_CHECK, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_MEASURE, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_LATERAL, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_VERIFY, "summed"),
        (PHASE_CLOUD_MEASURE, "summed"),
    ],
)
def test_a_banked_take_records_the_kind_its_phase_actually_played(
    tmp_path: Path, phase: str, expected_kind: str,
) -> None:
    """CHECK, MEASURE and LATERAL play ONE recording that steps through every
    driver in turn, which is neither a single driver nor a simultaneous sum.
    They were banked as ``summed`` only because the taxonomy had no third
    value; now they are banked as what they are. A lateral pose belongs with
    the other two because ``programs.program_for_phase`` answers it with
    MEASURE's program OBJECT verbatim — the same stimulus under a third name.
    VERIFY and the cloud position groups really do play one summed sweep and
    keep the old label.
    """

    from jasper.web import correction_crossover_v2 as v2host

    store, bundle_dir = _open_store(tmp_path)
    bank = v2host.bind_position_retention(store, CAPTURE, {}, asyncio.run)

    class _Result:
        wav = b"take-bytes"

    # A lateral pose names its prompted spot ``pose_id``; every other phase
    # calls it ``position_id``. The two vocabularies ``spatial._take_identity``
    # keeps apart, so the lateral row drives the shape a pose really banks.
    id_key = "pose_id" if phase == PHASE_LATERAL else "position_id"
    bank(
        _Result(),
        {
            id_key: f"{phase}_00",
            "phase": phase,
            "index": 0,
            "attempt": 1,
            "take_id": f"{phase}_00_a01",
            "measure_kind": "",
            "prompt": "",
            "wide": False,
            "captured_at": 1.0,
            "session_id": CAPTURE,
            "wav_sha256": "d" * 64,
        },
    )

    info = json.loads((bundle_dir / "info.json").read_text())
    # Every kind here shares one list — the recorded kind is the only thing
    # that tells the three apart, which is why it is written at all.
    assert [e["kind"] for e in info["summed_captures"]] == [expected_kind]
    assert info["captures"] == []


# --------------------------------------------------------------------------- #
# Corpus acceptance — P2 becomes free, measured on real captures
# --------------------------------------------------------------------------- #


@corpus.requires_s0_curves
def test_the_persisted_block_alone_answers_p2_on_the_s0_corpus() -> None:
    """The claim §5 makes about P2 — "Free — *once the harness persists
    per-position curves*; today it needs raw WAVs off the Pi" — measured on
    the corpus that forced the exception.

    WO-0 had to rebuild M2's rung frequencies, M5's LF dips, and the whole
    Fc-notch series from raw WAVs pulled off the Pi, because the flow reported
    ``n_confident`` and ``clustered_fraction`` and discarded the distribution
    behind them. This asserts the persisted block now carries that
    distribution: the per-position taus are recoverable exactly, and their CV
    — P2's whole classifier — is computable from the block with no WAV in
    sight.

    Regime: the S0 main leg, ten positions on a desk at ~1 m, whose comb S0
    attributed to a source-fixed reflection by physically relocating the
    speaker. It is the one corpus where a LOCKED verdict is known ground
    truth.
    """

    positions = [
        cloud_position_capture(p) for p in _s0_cloud_positions(corpus.S0_MAIN)
    ]
    combined = combine_positions(positions)
    assert combined.geometry.locked is True

    block = position_evidence_block(
        combined,
        position_records=[
            {"position_id": p.position_id, "index": i, "attempt": 1,
             "take_id": f"{p.position_id}_a01"}
            for i, p in enumerate(positions)
        ],
    )

    assert block["available"] is True
    rows = block["positions"]
    assert len(rows) == combined.geometry.n_positions == 10

    # 1. Every per-position tau survives EXACTLY — not rounded, not averaged.
    persisted = [
        row["echo"]["tau_us"] for row in rows if row["echo"] is not None
    ]
    live = [
        echo.tau_us for echo in combined.per_position_echo if echo is not None
    ]
    assert persisted == live

    # 2. P2's classifier is computable from the block alone. The usable
    #    estimates are the ones with no refusal and a real confidence — the
    #    same admission the geometry lock applies — and their dispersion is
    #    what "source-fixed vs position-variant" is decided on.
    usable = [
        row["echo"]["tau_us"]
        for row in rows
        if row["echo"] is not None
        and row["echo"]["refusal"] == ""
        and row["echo"]["confidence"] > 0.0
        and row["echo"]["tau_us"] > 0.0
    ]
    assert len(usable) >= combined.geometry.n_confident
    mean_tau = sum(usable) / len(usable)
    assert mean_tau > 0.0
    cv = (
        math.sqrt(sum((t - mean_tau) ** 2 for t in usable) / len(usable))
        / mean_tau
    )
    # This corpus's comb is the source-fixed one; the point is not the exact
    # figure but that a figure exists at all from persisted data.
    assert 0.0 < cv < 0.5

    # 3. The member curves are genuinely per-position, not ten copies of the
    #    aggregate — the failure mode that would make the whole block a
    #    convincing-looking no-op.
    curves = [row["magnitude_db"] for row in rows]
    assert len({tuple(curve) for curve in curves}) == len(curves)


def _s0_cloud_positions(session_dir) -> list:
    """The S0 leg reassembled the way the live conductor assembles a group."""

    from jasper.active_speaker.crossover_v2_flow import _CloudPosition

    positions = []
    ids = sorted(corpus.S0_MAIN_TWEETER_HEIGHT + corpus.S0_MAIN_HAND_WIDTH_LOW)
    for index, position_id in enumerate(ids):
        response, _fc_hz = corpus.s0_position_driver_response(
            session_dir, position_id
        )
        positions.append(
            _CloudPosition(
                position_id=position_id,
                index=index,
                attempt=1,
                prompt="(corpus replay)",
                wide=False,
                captured_at=0.0,
                response=response,
                sample_rate_hz=48_000,
            )
        )
    return positions
