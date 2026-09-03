# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a endpoint binding: the v2 host, its status projection, and the conductor.

Integration tests drive :mod:`jasper.web.correction_crossover_v2`'s apply,
recovery and evidence-retention handlers and
:mod:`jasper.web.correction_crossover_v2_status`'s projection against a REAL
``CrossoverV2Session`` conductor and REAL evidence store — no network, no
phone driver, no wire protocol.

Route registration + CSRF ordering ride the existing exact-surface contract
test (tests/test_web_correction_setup.py::test_known_post_routes_reach_csrf_guard,
which drives every ``_POST_ROUTES`` entry — now including the three
``/crossover/v2/*`` routes — to the CSRF guard); this file adds the
flow-selector refusals the dispatch relies on.
"""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import hashlib
import inspect
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.driver_protection import (
    PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2_flow import (
    STAGE1_INCLUDES_CLOUD_MEASURE,
    TIER_EXPRESS,
    TIER_FULL,
    V2_FIRST_BEGIN_TIMEOUT_S,
    CrossoverV2Session,
    V2FlowSeams,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    build_v2_verify_session_spec,
    resolve_plan_shape,
    v2_first_begin_timeout_s,
)
import jasper.active_speaker.baseline_profile as baseline_profile_mod

import jasper.capture_protocol as capture_protocol
from jasper.capture_protocol import MAX_TTL_S
from jasper.web import correction_crossover_backend
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status
from jasper.web.correction_crossover_v2_wired import WiredCaptureAnswer

from tests.conftest import seat_process_volume_owner
from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    SESSION_VOLUME_DB,
    _preset,
    _roles,
)

_BINDING = "placement_abcdefghijklmnopqrstuv"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH",
        str(tmp_path / "model_error.json"),
    )
    v2host.reset_session_measurement_pause_for_tests()
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)
    v2host.reset_session_measurement_pause_for_tests()


def _bg_run_async(coro, *, timeout=None):
    """Mimic correction_setup._run_async for the host recovery helpers: run the
    coroutine to completion and return its result (each on a fresh loop — the
    session-volume drains are self-contained, no cross-loop context manager)."""
    return asyncio.run(coro)


def test_live_model_error_binding_reports_identity_conflict_to_conductor():
    observation = {
        "speaker_id": "speaker-a",
        "attempt_id": "candidate-a",
        "metric": "max_db_notch_excluded",
        "predicted_db": 0.0,
        "realized_db": 0.9,
        "context": {"session_id": "session-a"},
    }

    assert v2host._record_live_model_error(**observation) is True
    assert v2host._record_live_model_error(
        **{**observation, "realized_db": 0.7},
    ) is False


class _NoGraphSession:
    """A tuning session that holds no graph, for the pause-lifecycle tests.

    ``_volume_hooks`` opens the session after the plan confirms and closes it
    where the graph used to go back. These tests are about the PAUSE, not the
    graph, so the session they pass holds no graph — which is also the real
    no-op shape for a session that never played a routed stimulus.

    It DOES give the claim back, and that is the session's real contract
    rather than a convenience: ``TuningSession.close`` releases the volume
    slot, and the plan's drain runs after it precisely so the claim is gone by
    then. A double that kept the claim would model a session that never closed.

    The drain it runs after would DEFER, not fail — a rank-1 claim outranking
    the household level means the level is recorded and lands on release. That
    is the code's behaviour, not the double's; the adversarial review's B1
    found this comment asserting the opposite, and the drains that genuinely
    run without a claim now stop on a deferral instead of walking to their
    emergency rung.
    """

    def __init__(self) -> None:
        # The hooks build the plan's door over THIS session's claim, so a
        # double standing in for a session carries a real one over the
        # process's owner — the same object the door establishes through.
        # Exposed as ``claim`` because the caller injects it into both, the
        # way production's composition root does; nothing reads it back out
        # of ``seams`` (that would be the engine-internal reach the
        # verification suite forbids).
        self.claim = _session_claim()
        self.seams = SimpleNamespace(volume=self.claim)

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        if self.seams.volume is not None:
            await self.seams.volume.release()


def _own_the_fader(monkeypatch, cam) -> None:
    """Seat a real ``VolumeOwner`` over ``cam`` for the drain paths.

    After W5-c1 the plan reaches the fader through the owner, so a drain test
    without one exercises the fail-closed no-owner door instead of the drain.
    """
    seat_process_volume_owner(
        monkeypatch,
        lambda db: cam.set_volume_db(db, best_effort=True),
        lambda: cam.get_volume_db(best_effort=True),
    )


def _session_claim():
    """The session's one claim over whatever owner this test installed."""
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
    )
    from jasper.volume_owner import volume_owner

    owner = volume_owner()
    return None if owner is None else MeasurementVolumeClaim(owner)


class _FakeVolCam:
    """A CamillaController stand-in for the session-volume drains."""

    def __init__(self, vol: float) -> None:
        self.vol = vol

    async def set(self, db: float) -> bool:
        self.vol = float(db)
        return True

    async def get(self) -> float:
        return self.vol

    async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
        self.vol = float(db)
        return True

    async def get_volume_db(self, best_effort: bool = False) -> float:
        return self.vol


def _live_measurement_session(
    monkeypatch,
    *,
    household_db: float = -15.0,
    measurement_db: float = -20.0,
    ceiling_s: float = 10.0,
):
    """A plan holding a LIVE rank-1 claim over a real owner, as a drain finds it.

    Every out-of-runner drain scenario needs the same four things standing up
    together — a real ``VolumeOwner`` over a fake fader, a genuinely held
    ``MeasurementVolumeClaim``, an opened plan, and that plan installed as the
    host's — because a double that merely LOOKS held exercises the no-claim
    door instead of the drain.

    ``household_db == measurement_db`` is the same-level case: the door's
    deferral test compares the level in effect against the level being
    restored, so equal levels answer LANDED under a live claim rather than
    DEFERRED.

    Returns ``(plan, cam, claim, clock)``; ``clock`` is a one-element list the
    caller advances to walk past the ceiling.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
        OwnerVolumeDoor,
    )
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )
    from jasper.volume_owner import volume_owner

    clock = [1000.0]
    plan = SessionVolumePlan(
        wall_clock_ceiling_s=ceiling_s, clock=lambda: clock[0],
    )
    cam = _FakeVolCam(household_db)
    _own_the_fader(monkeypatch, cam)
    owner = volume_owner()
    claim = MeasurementVolumeClaim(owner)
    opened = asyncio.run(
        plan.open(
            measurement_db,
            OwnerVolumeDoor(
                owner, read_fader=cam.get_volume_db, claim=claim,
            ),
        )
    )
    assert opened is SessionVolumeOpenResult.OPENED
    assert cam.vol == measurement_db
    v2host.set_volume_plan_for_tests(plan)
    return plan, cam, claim, clock


@contextlib.contextmanager
def _stage2_openable():
    """Satisfy the apply's stage-2 openability preflight (work order D3).

    ``handle_v2_apply`` runs ``resolve_conductor_context`` immediately before
    the transaction commits — PR-T3's half of the preflight, the one that
    catches a household applying from a stale page or a second tab. Tests whose
    subject is the apply TRANSACTION stub the predicate to a pass so they keep
    testing what they are about; the preflight's own behaviour (refusal copy,
    fail-closed on an unexpected error, ordering after the freshness gates) has
    its own tests.
    """
    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: object()
    try:
        yield
    finally:
        v2host.resolve_conductor_context = original


def _apply(raw, run_async, camilla_factory, *, status=None):
    """``handle_v2_apply`` with the stage-2 preflight satisfied."""
    with _stage2_openable():
        return v2host.handle_v2_apply(
            raw, run_async, camilla_factory, status={} if status is None else status,
        )


# --- the first-begin budget knob (#2637) ---------------------------------------
#
# Four commissioning sessions on the 2026-08-16 walk died at exactly the 300 s
# default in phase=awaiting_begin, so the budget is a jasper.env edit rather than
# a rebuild. The reader is the flow's, the wiring is this host's, and both are
# pinned here so the pair cannot drift apart.


def test_the_first_begin_budget_defaults_to_the_constant(monkeypatch):
    """Unset env ⇒ the shipped 300 s, read off the constant itself."""
    monkeypatch.delenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", raising=False)
    assert v2_first_begin_timeout_s() == V2_FIRST_BEGIN_TIMEOUT_S == 300.0


def test_the_first_begin_budget_takes_an_in_range_override(monkeypatch):
    monkeypatch.setenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", "900")
    assert v2_first_begin_timeout_s() == 900.0


@pytest.mark.parametrize(
    "raw",
    [
        "",           # present but empty — an operator who blanked the line
        "   ",
        "soon",       # unparseable
        "29.9",       # below the 30 s floor
        "99999",      # above the ceiling
    ],
)
def test_a_bad_first_begin_value_falls_back_to_the_default(monkeypatch, raw):
    """A jasper.env typo can never shorten or brick the first-begin window.

    Same fall-back idiom as every other ``bounded_env_float`` knob — the value
    is dropped silently, not raised, because the alternative is a commissioning
    flow that refuses to start over a stray character.
    """
    monkeypatch.setenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", raw)
    assert v2_first_begin_timeout_s() == V2_FIRST_BEGIN_TIMEOUT_S


def test_the_first_begin_ceiling_is_the_session_ceiling(monkeypatch):
    """The ceiling IS ``MAX_TTL_S``, not a copy of it that agrees today.

    ``.env.example`` tells an operator the 3600 s bound is the longest link the
    the transport grants, so nothing above it can mean anything on any stage.
    That sentence is only true while the reader derives its ceiling from
    ``MAX_TTL_S`` — a hard-coded twin would pass every other test in this file
    and make the disclosure a lie the day either number moved.

    So the last two lines MOVE THE OWNER rather than trusting the numbers to
    agree. The reader takes ``MAX_TTL_S`` through a function-local import, so
    the lookup happens per call and a patched owner is genuinely what it reads —
    which is the whole justification for that import being function-local
    instead of joining the top-level one. A twin answers the default here.
    """
    monkeypatch.setenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", str(MAX_TTL_S))
    assert v2_first_begin_timeout_s() == float(MAX_TTL_S)
    monkeypatch.setenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", str(MAX_TTL_S + 1))
    assert v2_first_begin_timeout_s() == V2_FIRST_BEGIN_TIMEOUT_S

    monkeypatch.setattr(capture_protocol, "MAX_TTL_S", 7200)
    monkeypatch.setenv("JASPER_V2_FIRST_BEGIN_TIMEOUT_S", "7000")
    assert v2_first_begin_timeout_s() == 7000.0  # a twin would answer 300.0


def test_the_env_example_ceiling_prose_tracks_max_ttl_s():
    """The operator-facing 3600 is prose, so only a test can keep it honest.

    ``.env.example`` states the ceiling twice — once as the advertised range and
    once as the sentence naming what the bound IS. Prose cannot be derived the
    way the reader's ``hi=`` is, so those are the two copies an OPERATOR reads,
    and the only ones this change leaves unguarded by the derivation itself.

    Deliberately a containment check, not a parse: the wording is free to be
    rewritten, the NUMBER is not free to disagree with its owner. **Scope, said
    plainly rather than implied:** this catches the block going stale as a whole
    — the case that actually happens, since ``MAX_TTL_S`` moving leaves both
    copies behind at once. It does NOT catch someone updating one copy and not
    the other, because a live number anywhere in the block satisfies it. That
    gap is left open rather than closed with a positional parse, which would
    pin the wording this test deliberately leaves free, and which needs two
    independent things to go wrong before it bites.

    **What the residual gap costs, stated straight rather than softened.** If
    ``MAX_TTL_S`` ever SHRINKS — it mirrors a separately released artifact, and
    a mirror tracks down as well as up — this guard fires, and a half-update
    that fixes only the advertised range leaves the other sentence quoting the
    old, HIGHER bound. An operator who believes it sets a value above the real
    ceiling, and nothing clamps that: ``bounded_env_float`` DROPS an
    out-of-range value and silently answers the 300 s default, which is the
    very failure this knob exists to prevent. (The Worker's clamp is on
    ``ttl_s`` mint requests and does not reach this knob.) Accepted because the
    likely direction — the owner growing, both copies left behind — is the one
    the assertion below catches outright.
    """
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    start = text.index("# JASPER_V2_FIRST_BEGIN_TIMEOUT_S")
    block = text[start:text.index("\nJASPER_V2_FIRST_BEGIN_TIMEOUT_S=", start)]
    assert str(MAX_TTL_S) in block, (
        "the .env.example ceiling prose no longer names MAX_TTL_S's value; "
        "an operator is being told a stale bound"
    )


def test_position_retention_survives_a_retake_through_the_real_evidence_store(
    tmp_path,
):
    """The retention seam against the REAL, write-once store — not a lambda.

    Round-1 review blocker B3: every other test substitutes a recorder for
    the retention seam, so nothing exercised the store's write-once contract.
    Against the real one, two takes of a retaken position used to collide on a
    single path: the second write was refused (fail-soft, so the session
    survived) and the ONLY surviving sidecar described the REPLACED take — its
    wav, its prompt — while the curve actually in the cloud had no record at
    all. That inverts the forensic honesty the retention bump was paid for.

    What this pins: both takes persist, under distinguishable paths, each
    describing itself.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    refs: dict = {}
    bank = v2host.bind_position_retention(
        store, "cap_retake_session", refs, asyncio.run,
    )

    position_id = f"{PHASE_CLOUD_MEASURE}_10"
    base = {
        "position_id": position_id, "phase": PHASE_CLOUD_MEASURE, "index": 10,
        "wide": False, "role": "onax", "captured_at": 1.0,
        "session_id": "cap_retake_session",
        # What ``spatial.take_kind`` stamps on every built record, and what the
        # store routes a position take by. Empty is the honest answer for a
        # take whose graph names no fingerprint, and it still banks.
        "measure_kind": "",
        "gate_window_ms": 8.0, "validity_floor_hz": 140.0,
        "gating_applied": True, "summed_ripple_db": 1.0,
        "glitch_detected": False,
    }
    bank(
        WiredCaptureAnswer(wav=b"first-take"),
        {**base, "attempt": 10, "take_id": f"{position_id}_a10",
         "prompt": "Move the microphone 10 in (25 cm) to the LEFT of the "
                   "mark, at mark height."},
    )
    bank(
        WiredCaptureAnswer(wav=b"wider-retake"),
        {**base, "attempt": 11, "take_id": f"{position_id}_a11",
         "wide": True, "role": "offax",
         "prompt": "Same measurement, wider spot: move the microphone "
                   "30 in (75 cm) to the LEFT of the mark."},
    )

    # Two artifacts, both published — the second is NOT a refused duplicate.
    assert [entry["attempt"] for entry in refs["position_artifacts"]] == [10, 11]
    assert len({entry["artifact"] for entry in refs["position_artifacts"]}) == 2

    # The strict store namespaces every artifact under evidence/v1/artifacts/.
    sidecars = sorted(
        (Path(info["bundle_dir"]) / "evidence" / "v1" / "artifacts"
         / "crossover_v2" / "cap_retake_session" / "positions").glob("*.json")
    )
    assert [p.name for p in sidecars] == [
        f"{position_id}_a10.json", f"{position_id}_a11.json",
    ]
    first, second = (json.loads(p.read_text()) for p in sidecars)
    # Each sidecar describes ITS OWN take: its prompt, its wav.
    assert first["attempt"] == 10 and second["attempt"] == 11
    assert "wider spot" in second["prompt"]
    assert "wider spot" not in first["prompt"]
    assert second["wide"] is True
    # The role rides the sidecar — it is the durable half of the promotion.
    assert first["role"] == "onax" and second["role"] == "offax"
    assert first["wav_path"] != second["wav_path"]
    for record in (first, second):
        wav = Path(info["bundle_dir"]) / record["wav_path"]
        assert wav.is_file() and wav.stat().st_size == record["wav_bytes"]
    assert (
        Path(info["bundle_dir"]) / first["wav_path"]
    ).read_bytes() == b"first-take"
    assert (
        Path(info["bundle_dir"]) / second["wav_path"]
    ).read_bytes() == b"wider-retake"


def test_retained_position_is_recorded_in_the_bundle_it_was_written_into(
    tmp_path,
):
    """A retained take is findable from the bundle's own metadata.

    Before this, the seam wrote the WAV straight to the bundle-relative path
    it minted and registered it nowhere: ``info.json`` reported
    ``summed_captures: []`` and ``artifact_manifest.json`` listed only
    ``info.json`` itself, so a bundle carrying tens of MB of real audio did
    not describe any of it.

    The oversize take is the second half of the pin: a real summed capture
    runs past ``append_capture``'s external-source size guard, so the
    recording route must be the one that does not apply it.
    """
    from jasper.active_speaker.bundles import MAX_CAPTURE_WAV_BYTES, open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    bundle_dir = Path(info["bundle_dir"])
    store = CommissioningEvidenceStore.open(
        bundle_dir, expected_session_id=info["session_id"]
    )
    refs: dict = {}
    bank = v2host.bind_position_retention(
        store, "cap_record_session", refs, asyncio.run,
    )

    oversize = b"\x00" * (MAX_CAPTURE_WAV_BYTES + 1)
    bank_id = bank(
        WiredCaptureAnswer(wav=oversize),
        {"position_id": f"{PHASE_CLOUD_MEASURE}_04",
         "take_id": f"{PHASE_CLOUD_MEASURE}_04_a04", "measure_kind": "",
         "phase": PHASE_CLOUD_MEASURE, "index": 4, "attempt": 4,
         "wide": False, "role": "onax", "captured_at": 1.0,
         "session_id": "cap_record_session", "prompt": "on the mark",
         "gate_window_ms": 8.0, "validity_floor_hz": 140.0,
         "gating_applied": True, "summed_ripple_db": 1.0,
         "glitch_detected": False},
    )

    assert bank_id, "the record must bank, or the WAV assertions below are vacuous"
    entries = json.loads((bundle_dir / "info.json").read_text())["summed_captures"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["group"] == f"{PHASE_CLOUD_MEASURE}_04_a04"
    assert (bundle_dir / entry["artifact_path"]).read_bytes() == oversize
    assert (bundle_dir / entry["capture_json_path"]).is_file()

    manifest = json.loads((bundle_dir / "artifact_manifest.json").read_text())
    recorded = {
        artifact["path"]: artifact for artifact in manifest["artifacts"]
    }
    assert entry["artifact_path"] in recorded
    assert entry["capture_json_path"] in recorded
    assert recorded[entry["artifact_path"]]["byte_size"] == len(oversize)
    assert recorded[entry["artifact_path"]]["sha256"] == hashlib.sha256(
        oversize
    ).hexdigest()


def _retention_bundle(tmp_path, capture_session_id, *, provenance=None):
    """A real bundle, a real evidence store, and the seam bound over both."""
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    bundle_dir = Path(info["bundle_dir"])
    store = CommissioningEvidenceStore.open(
        bundle_dir, expected_session_id=info["session_id"]
    )
    refs: dict = {}
    bank = v2host.bind_position_retention(
        store, capture_session_id, refs, asyncio.run, provenance=provenance,
    )
    return bank, refs, bundle_dir, store


def _entry_baseline_take(index=9, attempt=1):
    """One entry-baseline record, from the builder the flow actually calls."""
    from jasper.active_speaker.crossover_v2.spatial import entry_baseline_record

    return entry_baseline_record(
        index=index,
        attempt=attempt,
        session_id="cap_entry_session",
        program_id="prog-entry",
        reference_mark="design_axis",
        graph_fingerprint="fp-entry",
        captured_at="2026-08-27T00:00:00Z",
        freqs_hz=[100.0, 200.0, 400.0],
        magnitude_db=[0.0, -1.0, -2.0],
        excluded=[False, False, True],
        validity_floor_hz=140.0,
        gate_window_ms=8.0,
        summed_ripple_db=1.0,
        glitch_detected=False,
        wav_sha256="d" * 64,
    )


def test_an_entry_baseline_banks_under_the_take_id_its_own_record_names(tmp_path):
    """The artifact's stem IS ``record["take_id"]`` — one mint, not two.

    An entry baseline is the one retained kind whose ``position_id`` is already
    a take id, so a seam that re-minted one from the position id appended a
    second ``_aNN``: the file landed at ``entry_baseline_09_a01_a01.json`` while
    the record inside said ``entry_baseline_09_a01``, and
    ``refs["position_artifacts"]`` carried the doubled id. Nothing was
    observed-broken, because every reader globs rather than reconstructing a
    filename — what was broken is the JOIN between a take and its artifact, and
    that join is what W1-d's index is built on.

    The store names the artifact from the record's own take id, so the fix falls
    out of the lift. Pinned anyway: "it fell out for free" is what gets un-fixed.
    """
    bank, refs, bundle_dir, _store = _retention_bundle(
        tmp_path, "cap_entry_session",
    )

    record = _entry_baseline_take()
    record_id = bank(WiredCaptureAnswer(wav=b"entry-bytes"), record)

    banked = sorted(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / "cap_entry_session" / "positions").glob("*.json")
    )
    assert [path.stem for path in banked] == [record["take_id"]]
    assert record_id.endswith(f"/{record['take_id']}.json")
    # ...and the state's own index names the same take, which is the half the
    # doubled mint actually corrupted.
    assert [e["take_id"] for e in refs["position_artifacts"]] == [
        record["take_id"]
    ]
    assert json.loads(banked[0].read_text())["take_id"] == record["take_id"]


def _stage_seams_over(store, capture_session_id, refs, recorder):
    """One stage's REAL seams, bound the way production binds them.

    ``bind_v2_stage_seams`` is the single owner of which callable fills which
    seam, and the provenance carry is a coupling BETWEEN two of them — so a
    pin that built the two binders itself would prove the halves work and
    never notice the day the binder stopped handing them the same recorder.
    """
    from jasper.active_speaker.crossover_v2.journey import (
        STAGE_MEASURE_CAPABILITIES,
        open_stage,
    )

    return v2host.bind_v2_stage_seams(
        open_stage(STAGE_MEASURE_CAPABILITIES, index_phase_map={}),
        play=lambda *_a, **_kw: None,
        evidence_store=store,
        capture_session_id=capture_session_id,
        refs=refs,
        publish_check=lambda *_a, **_kw: None,
        publish_candidate=lambda *_a, **_kw: None,
        run_async=asyncio.run,
        camilla_factory=None,
        provenance=recorder,
    )


def test_the_banked_take_carries_the_provenance_the_analyze_seam_carried(
    tmp_path, monkeypatch,
):
    """Obligation 4: the single shot is re-homed, not dropped.

    ``provenance.take()`` used to have exactly one consumer — the capture-dump
    ring's writer — and that ring is gone. Deleting the only consumer without
    re-homing the shot would have made the recorder write-only and lost the
    graph a capture went through, and it would have done that while PASSING
    EVERY TEST, because nothing asserted a banked take carries provenance.
    This is that assertion, and it is why this pin was written before the code
    it guards.

    Driven through ``bind_v2_stage_seams`` so all three halves are covered: the
    analyze seam carrying the shot forward, the banking seam draining it, and
    the binder handing both the SAME recorder.

    The values ORIGINATE at the play seam, observed while the stimulus was
    emitting. This pin seeds the recorder directly rather than driving a real
    play — the observation itself is ``record_capture_provenance``'s own
    subject — so what it asserts is the CARRY: that the value the play seam
    left reaches the banked record unchanged, and never a fresh reading.

    Note what the seeding stands in for: in an ordinary session the play seam
    feeds the recorder on every capture it holds one for, with nothing to arm.
    ``tests/test_capture_provenance.py`` drives that half through the real play
    seam; a banked take with no ``provenance`` key now means the take was
    banked with no analyze behind it.
    """
    from jasper.active_speaker.capture_provenance import (
        CaptureProvenance,
        CaptureProvenanceRecorder,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_ENTRY_BASELINE
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture",
        lambda *_a, **_kw: "analysis",
    )

    _bank, refs, bundle_dir, store = _retention_bundle(
        tmp_path, "cap_prov_session",
    )
    recorder = CaptureProvenanceRecorder()
    seams = _stage_seams_over(store, "cap_prov_session", refs, recorder)

    # What the play seam saw while this capture's stimulus was emitting.
    recorder.record(CaptureProvenance(
        graph_kind="measurement",
        main_volume_db=-20.0,
        session_volume_db=-20.0,
        graph_fingerprint="fp-at-play",
        stimulus_program_id="prog-entry",
        stimulus_wav_sha256="c" * 64,
    ))

    seams.analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        _FakeResult(),
        MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase=PHASE_ENTRY_BASELINE,
    )
    record = _entry_baseline_take()
    assert seams.bank_take(WiredCaptureAnswer(wav=b"entry-bytes"), record)

    banked = json.loads(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / "cap_prov_session" / "positions" / f"{record['take_id']}.json"
         ).read_text()
    )
    carried = banked["provenance"]
    assert carried["graph"]["kind"] == "measurement"
    assert carried["graph"]["fingerprint"] == "fp-at-play"
    assert carried["main_volume_db"] == -20.0
    assert carried["session_volume_db"] == -20.0
    assert carried["stimulus"]["program_id"] == "prog-entry"
    # The PLAYED program's digest is lifted to its own top-level column, so a
    # reader joining takes by stimulus need not open the provenance block —
    # and it never displaces ``wav_sha256``, which is the CAPTURED audio's.
    assert banked["stimulus_wav_sha256"] == "c" * 64
    assert banked["wav_sha256"] != banked["stimulus_wav_sha256"]


def test_a_capture_that_observed_nothing_never_inherits_the_last_one_s_graph(
    tmp_path, monkeypatch,
):
    """B1: a refused capture must not leave its provenance for the next one.

    Banking is accepted-only, so a REFUSED capture's analyze parks a value in
    the carry that nobody takes out. If the next accepted capture's own
    observation missed — a marker flipped mid-session, or the blind belt ate a
    CamillaDSP hiccup — it would drain that stranded value and write it into a
    write-once forensic record, naming the graph and the fader of a capture
    that never became evidence.

    ``record`` cannot clear it: the case that strands a value is exactly the
    case where there is no new value to overwrite it with. So the carry is
    drained unconditionally at every analyze, and this drives that sequence —
    observe, refuse (nothing banks), observe NOTHING, accept — and requires the
    accepted take to name no provenance rather than the refused one's.
    """
    from jasper.active_speaker.capture_provenance import (
        CaptureProvenance,
        CaptureProvenanceRecorder,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_ENTRY_BASELINE
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture", lambda *_a, **_kw: "analysis",
    )

    _bank, refs, bundle_dir, store = _retention_bundle(
        tmp_path, "cap_stale_session",
    )
    recorder = CaptureProvenanceRecorder()
    seams = _stage_seams_over(store, "cap_stale_session", refs, recorder)

    def _analyze_once():
        seams.analyze(
            build_verify_program(FC_HZ, sweep_s=0.5),
            _FakeResult(),
            MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase=PHASE_ENTRY_BASELINE,
        )

    # 1. A capture that WAS observed — and then refused, so nothing banks it.
    recorder.record(CaptureProvenance(
        graph_kind="measurement", graph_fingerprint="fp-of-the-refused-take",
    ))
    _analyze_once()

    # 2. The next capture observes nothing at all, and is accepted.
    _analyze_once()
    accepted = _entry_baseline_take(index=11)
    assert seams.bank_take(WiredCaptureAnswer(wav=b"accepted-bytes"), accepted)

    banked = json.loads(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / "cap_stale_session" / "positions" / f"{accepted['take_id']}.json"
         ).read_text()
    )
    assert "provenance" not in banked


def test_a_take_with_no_play_behind_it_names_no_provenance(
    tmp_path, monkeypatch,
):
    """The drain is single-shot, for the reason the recorder already gives.

    Stale provenance on a forensic record is worse than absent: absent is
    visibly absent. A bank with no analyze between it and the previous one is
    not a second capture of the same stimulus — it is a capture the recorder
    cannot speak for, and it names nothing rather than the last one's graph.
    """
    from jasper.active_speaker.capture_provenance import (
        CaptureProvenance,
        CaptureProvenanceRecorder,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_ENTRY_BASELINE
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture",
        lambda *_a, **_kw: "analysis",
    )

    _bank, refs, bundle_dir, store = _retention_bundle(
        tmp_path, "cap_drain_session",
    )
    recorder = CaptureProvenanceRecorder()
    seams = _stage_seams_over(store, "cap_drain_session", refs, recorder)

    recorder.record(CaptureProvenance(graph_kind="measurement"))
    seams.analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        _FakeResult(),
        MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase=PHASE_ENTRY_BASELINE,
    )
    seams.bank_take(WiredCaptureAnswer(wav=b"first"), _entry_baseline_take(index=9))

    # No play and no analyze — so nothing this second take may claim.
    second = _entry_baseline_take(index=10)
    assert seams.bank_take(WiredCaptureAnswer(wav=b"second"), second)

    banked = json.loads(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / "cap_drain_session" / "positions" / f"{second['take_id']}.json"
         ).read_text()
    )
    assert "provenance" not in banked


#: The frames the page says it recorded, against the ``RECEIVED_FRAMES`` the
#: host decodes below: one render quantum short of arriving, which is the
#: 2026-08-03 loss shape and the reason the ledger exists.
DECLARED_FRAMES = 4800
RECEIVED_FRAMES = DECLARED_FRAMES - 128


def _lossy_page_report():
    """A page report the host's own count disagrees with — a real defect."""
    return {
        "frames": DECLARED_FRAMES, "encoded_frames": DECLARED_FRAMES,
        "block_gaps": 0, "block_gap_frames": 0, "zero_run_count": 0,
    }


def _analysis_double(epsilon_ppm=1.25):
    """An analysis the REAL summary and the REAL ledger can both be run over.

    Deliberately not a ``ProgramAnalysis``: what these pins are about is the
    CARRY, and ``analysis_diagnostic_summary`` is duck-typed by contract (its
    own docstring names this file's stubbing as the reason). The numbers it
    reads are real ones, so the block it produces is really computed rather
    than a literal a stub handed back.
    """
    return SimpleNamespace(
        phase=PHASE_MEASURE,
        drift=SimpleNamespace(
            epsilon_ppm=epsilon_ppm,
            max_residual_samples=0.5,
            repeat_level_delta_db=0.25,
            glitch_detected=False,
            glitch_inputs=(),
            discontinuity_samples=0.0,
            discontinuity_after_segment="",
            per_role_epsilon_ppm={},
        ),
    )


def _bank_one_analyzed_take(
    tmp_path, monkeypatch, capture, *, report, epsilon_ppm=1.25, index=9,
):
    """Analyze one capture through the REAL seams, then bank it. Returns both.

    Driven through ``bind_v2_stage_seams`` for the reason the provenance pins
    above give: the carry is a coupling BETWEEN two seams, so a pin that built
    the two binders itself would never notice the day the binder stopped
    handing them the same slot.
    """
    from jasper.active_speaker.capture_provenance import CaptureProvenanceRecorder
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    analysis = _analysis_double(epsilon_ppm=epsilon_ppm)

    def _analyze_program_capture(
        _program, samples, _rate, *, capture_report=None, **_kw
    ):
        # The REAL reconciliation, over the frames this host really decoded
        # and the counters the page really reported — the one number in the
        # block set that a stub could not stand in for.
        analysis.frame_ledger = reconcile_capture_frames(
            capture_report, received_frames=len(samples),
        )
        return analysis

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture", _analyze_program_capture,
    )

    _bank, refs, bundle_dir, store = _retention_bundle(tmp_path, capture)
    seams = _stage_seams_over(store, capture, refs, CaptureProvenanceRecorder())
    result = _FakeResult(capture_integrity=report)
    # One quantum short of what the page declared: the host's count is the
    # WAV's own, so the ledger below reconciles two real numbers.
    result.wav = _mono_wav_bytes(RECEIVED_FRAMES)
    seams.analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        result,
        MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase=PHASE_MEASURE,
    )
    record = _entry_baseline_take(index=index)
    assert seams.bank_take(WiredCaptureAnswer(wav=b"analyzed-bytes"), record)
    banked = json.loads(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / capture / "positions" / f"{record['take_id']}.json").read_text()
    )
    return banked, analysis


def test_a_banked_take_carries_the_three_blocks_the_analysis_computed(
    tmp_path, monkeypatch,
):
    """The data-loss window the dump ring's death opened, closed.

    ``diagnostic``, ``capture_integrity`` and ``frame_ledger`` were the ring
    sidecar's, and #3250 deleted the ring — since then the analyze seam has
    computed all three and dropped them, so a round banked from here on could
    not be graded on frame loss at all. The banked record is the only
    retention path there is, so it carries them.

    Real content, not presence: the ledger is the REAL
    ``reconcile_capture_frames`` over the frames this host decoded against the
    counters the page reported, and it names the losing hop. A carry that
    passed the blocks through unchanged from some earlier capture, or wrote a
    placeholder, fails on those numbers rather than on a key check.
    """
    from jasper.audio_measurement.frame_ledger import LOST_AT_ENCODER_TO_HOST

    report = _lossy_page_report()
    banked, analysis = _bank_one_analyzed_take(
        tmp_path, monkeypatch, "cap_blocks_session", report=report,
    )

    # The recorder's own counters, verbatim — the checker's read set.
    assert banked["capture_integrity"] == report
    # The reconciliation, and the hop it names: the page encoded a quantum
    # this host never received, so the loss is real and attributed.
    ledger = banked["frame_ledger"]
    assert ledger == analysis.frame_ledger.to_dict()
    assert ledger["encoded_frames"] == DECLARED_FRAMES
    assert ledger["received_frames"] == RECEIVED_FRAMES
    assert ledger["lost_at"] == [LOST_AT_ENCODER_TO_HOST]
    # The flat numeric account, computed over this analysis and no other.
    diagnostic = banked["diagnostic"]
    assert diagnostic["phase"] == PHASE_MEASURE
    assert diagnostic["epsilon_ppm"] == 1.25
    assert diagnostic["frames_received"] == RECEIVED_FRAMES


def test_an_unmeasurable_diagnostic_never_costs_the_whole_take_record(
    tmp_path, monkeypatch,
):
    """A ``NaN`` in one block nulls that field, never the record.

    The evidence store canonicalises with ``allow_nan=False`` and the
    retention seam fail-softs, so one unmeasurable number reaching the record
    unscrubbed would refuse the write and lose the take entirely — the take
    id, the digest, the pose, everything — over a diagnostic. That is worse
    than the loss this change exists to stop, so the value becomes ``null``
    while the record banks.

    ``null`` and not a removed key: the field's absence is its own answer
    elsewhere in this block, so see
    :func:`test_the_scrub_nulls_a_bad_number_without_flattening_a_tri_state`.
    """
    banked, _analysis = _bank_one_analyzed_take(
        tmp_path, monkeypatch, "cap_nan_session",
        report=_lossy_page_report(), epsilon_ppm=float("nan"),
    )

    assert banked["diagnostic"]["epsilon_ppm"] is None
    # The record itself is intact, blocks and all.
    assert banked["diagnostic"]["phase"] == PHASE_MEASURE
    assert banked["frame_ledger"]["received_frames"] == RECEIVED_FRAMES
    assert banked["take_id"]


def test_the_scrub_nulls_a_bad_number_without_flattening_a_tri_state():
    """Both directions, because the two answers are different facts.

    ``analysis_diagnostic_summary`` spends ``None`` deliberately —
    ``polarity_agrees_with_sum`` is ``None`` for "nobody cross-checked" where
    an ABSENT key means "no alignment estimate at all", and the ``frame_*``
    terms are "present with ``None`` when the comparison ran but no frame
    could be fitted; absent only when no comparison happened". A scrub that
    dropped empty keys would collapse those two into one on a WRITE-ONCE
    record, so the distinction could never be recovered.

    So: an unbankable number becomes ``null`` and an explicit ``null``
    survives as a key. Nested, because the blocks are documents rather than
    flat rows.
    """
    scrubbed = v2host._bankable({
        "epsilon_ppm": float("nan"),
        "overflowed": float("inf"),
        "polarity_agrees_with_sum": None,
        "frame": {"tilt_db": None, "max_db": -3.5},
        "lost_at": ["encoder->host"],
        "declared_frames": 4800,
        "glitch_detected": False,
        "phase": "measure",
    })

    # The scrub's own half: unbankable numbers stop being numbers.
    assert scrubbed["epsilon_ppm"] is None
    assert scrubbed["overflowed"] is None
    # The tri-state's half: an explicit None is an ANSWER, and it keeps its key.
    assert "polarity_agrees_with_sum" in scrubbed
    assert scrubbed["polarity_agrees_with_sum"] is None
    assert scrubbed["frame"] == {"tilt_db": None, "max_db": -3.5}
    # Everything bankable is untouched, ``False`` and ``0`` included.
    assert scrubbed["lost_at"] == ["encoder->host"]
    assert scrubbed["declared_frames"] == 4800
    assert scrubbed["glitch_detected"] is False
    assert scrubbed["phase"] == "measure"


@pytest.mark.parametrize(
    "analysis, survives, lost",
    [
        # The two shapes the summary's own top-level defence already covers:
        # nothing raises, so nothing is lost and nothing is disclosed.
        pytest.param(object(), {"diagnostic"}, 0, id="foreign-object"),
        pytest.param("analysis", {"diagnostic"}, 0, id="bare-string"),
        # A sub-object that EXISTS makes the nested reads bare:
        # ``drift.epsilon_ppm`` raises AttributeError.
        pytest.param(
            SimpleNamespace(phase="measure", drift=SimpleNamespace()),
            set(), 1, id="drift-with-no-fields",
        ),
        # ``to_dict`` is a method call, not a getattr default — and the
        # summary reads the same ledger, so BOTH blocks are lost here.
        pytest.param(
            SimpleNamespace(phase="measure", frame_ledger=object()),
            set(), 2, id="ledger-with-no-to-dict",
        ),
        # ``math.isfinite`` over a pilot's ``snr_db`` raises TypeError on a
        # non-number: the guard's second caught type, exercised for real.
        pytest.param(
            SimpleNamespace(
                phase="measure",
                pilots=[SimpleNamespace(role="woofer", snr_db="not-a-number")],
            ),
            set(), 1, id="pilot-snr-that-is-not-a-number",
        ),
    ],
)
def test_building_the_blocks_never_costs_the_capture(
    analysis, survives, lost, caplog,
):
    """A half-populated analysis loses a BLOCK, never the measurement.

    The deleted ring writer wrapped this whole computation in a guard whose
    reason it stated outright — *"ANY failure here must never affect the
    measurement itself"* — and the belt has to survive the move, because the
    computation moved somewhere stricter: it now runs inside the analyze seam
    on the accepted path, where a raise costs the CAPTURE. The sweep already
    played and the operator is already standing at the mark.

    ``analysis_diagnostic_summary`` is defensive at its TOP level and bare
    below it — the first two rows are why the top-level defence is not enough
    to lean on, and the last three are the real shapes that get past it. Each
    is asserted by what survives rather than by "it did not raise", so a guard
    that swallowed a block it should have banked fails here too.

    A lost block is DISCLOSED and never silent: it is forensic evidence going
    missing, and the count is asserted so losing two blocks cannot read as
    losing one.
    """
    with caplog.at_level(logging.WARNING):
        blocks = v2host._capture_evidence_blocks(_FakeResult(), analysis)

    assert set(blocks) == survives
    assert all(isinstance(value, dict) for value in blocks.values())
    assert caplog.text.count(
        "event=correction.crossover_v2_capture_evidence_block_failed"
    ) == lost


def test_a_take_with_no_analyze_behind_it_carries_no_blocks(
    tmp_path, monkeypatch,
):
    """Single-shot, for the reason the provenance drain already gives.

    A second bank with no analyze between is a capture the analyze seam
    cannot speak for. Stale blocks on a write-once forensic record are worse
    than absent ones: absent is visibly absent, where a stale ``frame_ledger``
    would credit this take with another capture's frame accounting.
    """
    from jasper.active_speaker.capture_provenance import CaptureProvenanceRecorder
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture",
        lambda *_a, **_kw: _analysis_double(),
    )
    _bank, refs, bundle_dir, store = _retention_bundle(
        tmp_path, "cap_no_analyze_session",
    )
    seams = _stage_seams_over(
        store, "cap_no_analyze_session", refs, CaptureProvenanceRecorder(),
    )
    seams.analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        _FakeResult(),
        MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase=PHASE_MEASURE,
    )
    seams.bank_take(WiredCaptureAnswer(wav=b"first"), _entry_baseline_take(index=9))

    second = _entry_baseline_take(index=10)
    assert seams.bank_take(WiredCaptureAnswer(wav=b"second"), second)

    banked = json.loads(
        (bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
         / "cap_no_analyze_session" / "positions" / f"{second['take_id']}.json"
         ).read_text()
    )
    assert not {"diagnostic", "capture_integrity", "frame_ledger"} & set(banked)


@pytest.mark.parametrize(
    "phase", [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
)
def test_an_unprompted_phase_take_reaches_the_real_store(tmp_path, phase):
    """The three new takes are ROUTABLE, not merely built.

    Their measurement kind is ``""`` — honestly unresolved, because a CHECK or
    a MEASURE is taken through whatever graph is live and ``take_kind`` refuses
    to guess. The store accepts that, but only under the ``measure_kind``
    spelling: it routes that key by PRESENCE, while ``kind`` is routed by
    membership in ``MEASURE_KINDS``, which ``""`` fails. A tidy-up that made
    the two symmetrical would leave every take of these three phases with no
    route, and the retention fail-soft would turn that into a WARN and silence
    — a phase that banks nothing looks exactly like a phase nobody captured.

    So this drives the REAL store rather than a recorder: routed, written, and
    readable back under the take id its own record names.
    """
    from jasper.active_speaker.crossover_v2.spatial import phase_capture_record

    bank, refs, bundle_dir, _store = _retention_bundle(
        tmp_path, f"cap_{phase}_session",
    )
    # A fingerprint of the shape production emits. ``""`` would ALSO produce an
    # empty measure kind, but by a second route — ``take_kind`` reads an
    # unnamed graph as unclassifiable — so anchoring there would let this pin
    # keep passing for the wrong reason the day a claim states the comparand.
    # ``ENTRY_GRAPH_FINGERPRINT_UNKNOWN`` is what the coordinator hands the
    # flow when it cannot name the applied profile, which is the live shape.
    from jasper.active_speaker.crossover_v2.contracts import (
        ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
    )

    record = phase_capture_record(
        phase=phase, index=3, attempt=1,
        session_id=f"cap_{phase}_session",
        graph_fingerprint=ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
        captured_at="2026-08-27T00:00:00Z",
        wav_sha256="e" * 64,
    )
    assert record["measure_kind"] == ""

    banked_id = bank(WiredCaptureAnswer(wav=b"unprompted-bytes"), record)
    assert banked_id, "an unresolved measure kind must still route"

    landed = (
        bundle_dir / "evidence" / "v1" / "artifacts" / "crossover_v2"
        / f"cap_{phase}_session" / "positions" / f"{record['take_id']}.json"
    )
    assert json.loads(landed.read_text())["phase"] == phase
    assert [e["take_id"] for e in refs["position_artifacts"]] == [
        record["take_id"]
    ]


def test_a_store_that_refuses_costs_a_warning_and_not_the_capture(
    tmp_path, caplog,
):
    """The fail-soft boundary, at the binding the lift moved it to.

    Evidence retention is forensics, never a gate: a full disk must not turn an
    acoustically-good capture into a retake. The store stays strict for every
    other caller — this drives it into a REAL refusal (two different payloads at
    one write-once path is ``PATH_CONFLICT``) rather than a raising fake, so the
    exception this catches is the one production can actually raise.
    """
    bank, refs, _bundle_dir, _store = _retention_bundle(
        tmp_path, "cap_refuse_session",
    )

    record = _entry_baseline_take()
    assert bank(WiredCaptureAnswer(wav=b"entry-bytes"), record)

    with caplog.at_level(logging.WARNING):
        answer = bank(
            WiredCaptureAnswer(wav=b"entry-bytes"),
            {**record, "summed_ripple_db": 9.0},
        )

    assert answer == ""
    assert "crossover_v2_position_retain_failed" in caplog.text
    # The refused take is absent from the index rather than present-but-wrong.
    assert len(refs["position_artifacts"]) == 1


def test_cloud_publisher_writes_one_artifact_per_group_through_the_real_store(
    tmp_path,
):
    """Flat-linearization plan PR-4's ``publish_cloud`` seam against the REAL,
    write-once evidence store — mirrors
    ``test_position_retention_survives_a_retake_through_the_real_evidence_store``
    above, proving the two-groups-in-one-session shape does not collide.

    This is the mechanism deviation ``bind_cloud_publisher`` documents: the
    work order's literal ``crossover_v2/<session>/cloud.json`` would be
    written TWICE in one real session (once per closed group), and the store
    refuses a repeated path — so each group gets its own
    ``<phase>.json`` artifact instead.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    refs: dict = {}
    publish_cloud = v2host.bind_cloud_publisher(store, "cap_cloud_session", refs)

    measure_result = {
        "available": True,
        "geometry": {"locked": True, "reason": "geometry_locked"},
        "null_registry": {"classification": "position_invariant"},
        "spec": {"overall_passed": False},
        "curve": {"freqs_hz": [100.0, 200.0], "magnitude_db": [-1.0, -2.0]},
    }
    verify_result = {
        "available": True,
        "geometry": {"locked": False, "reason": "geometry_insufficient_usable_estimates"},
        "null_registry": {"classification": "insufficient_evidence"},
        "spec": {"overall_passed": True},
        "curve": {"freqs_hz": [100.0, 200.0], "magnitude_db": [-0.5, -0.6]},
    }
    publish_cloud(PHASE_CLOUD_MEASURE, measure_result)
    publish_cloud(PHASE_CLOUD_VERIFY, verify_result)

    # Both artifacts published, both fingerprints recorded — no collision.
    assert set(refs["cloud_artifacts"]) == {PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY}
    assert (
        refs["cloud_artifacts"][PHASE_CLOUD_MEASURE]
        != refs["cloud_artifacts"][PHASE_CLOUD_VERIFY]
    )

    artifacts_dir = (
        Path(info["bundle_dir"]) / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / "cap_cloud_session"
    )
    # One CLOUD artifact per closed group, distinctly named — the claim this
    # test exists for. Attribution's per-phase finding set (WO-1) rides in the
    # same directory under its own `findings_` prefix and shares the same
    # per-phase rule, so it is named here rather than allowed to widen the
    # assertion into "whatever happens to be on disk".
    assert sorted(p.name for p in artifacts_dir.glob("*.json")) == [
        f"{PHASE_CLOUD_MEASURE}.json",
        f"{PHASE_CLOUD_VERIFY}.json",
        f"findings_{PHASE_CLOUD_MEASURE}.json",
        f"findings_{PHASE_CLOUD_VERIFY}.json",
    ]
    measure_on_disk = json.loads(
        (artifacts_dir / f"{PHASE_CLOUD_MEASURE}.json").read_text()
    )
    assert measure_on_disk["kind"] == "jts_crossover_v2_cloud_evidence"
    assert measure_on_disk["capture_session_id"] == "cap_cloud_session"
    assert measure_on_disk["phase"] == PHASE_CLOUD_MEASURE
    assert measure_on_disk["geometry"]["locked"] is True
    assert measure_on_disk["null_registry"]["classification"] == "position_invariant"
    assert measure_on_disk["curve"]["freqs_hz"] == [100.0, 200.0]
    verify_on_disk = json.loads(
        (artifacts_dir / f"{PHASE_CLOUD_VERIFY}.json").read_text()
    )
    assert verify_on_disk["geometry"]["locked"] is False
    assert verify_on_disk["spec"]["overall_passed"] is True


def test_state_cloud_block_is_the_compact_projection_of_the_durable_pipeline():
    """PR-4's ``/state`` surface: per band, only ``passed``; the
    excluded-interval COUNT, not the intervals; the geometry verdict's two
    household-relevant bits. The full per-null τ/r/evidence numbers stay in
    the durable state's own ``pipeline`` sub-key (not re-derived here) and
    the bundle artifact — this is the dashboard-sized read, not a third
    owner of the same data."""
    v2host.save_v2_state({
        "session_id": "cap_state",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
                "positions": [],
                "pipeline": {
                    "available": True,
                    "merged_excluded_bands_hz": [[8000.0, 9000.0], [11000.0, 12000.0]],
                    "spec": {
                        "overall_passed": False,
                        "reference_db": -27.27,
                        "bands": [
                            {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True,
                             "graded_lo_hz": 357.14, "graded_hi_hz": 2000.0,
                             "max_deviation_db": 1.02, "max_deviation_hz": 412.0,
                             "tolerance_db": 1.5},
                            {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": True,
                             "graded_lo_hz": 2000.0, "graded_hi_hz": 8000.0,
                             "max_deviation_db": -1.41, "max_deviation_hz": 5100.0,
                             "tolerance_db": 2.0},
                            # The top band graded past its NOMINAL 16 kHz edge:
                            # this session's microphone is trusted to 20 kHz.
                            {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False,
                             "graded_lo_hz": 8000.0, "graded_hi_hz": 20000.0,
                             "max_deviation_db": -4.85, "max_deviation_hz": 11480.0,
                             "tolerance_db": 2.5},
                        ],
                    },
                    "flatness": {
                        "max_db": -4.85, "max_hz": 11480.0,
                        "max_band_hz": [8000.0, 16000.0], "tolerance_db": 2.5,
                        "rms_db": 1.37, "n_bins": 900, "n_excluded": 42,
                        "evaluable": True, "passed": False,
                    },
                    "validity_floor_hz": 187.5,
                },
            },
            PHASE_CLOUD_VERIFY: {
                "geometry": {"locked": False, "reason": "geometry_insufficient_usable_estimates"},
                "positions": [],
                "pipeline": {"available": False, "reason": "combine_failed"},
            },
        },
    })

    block = v2status.crossover_v2_status_block()
    cloud = block["cloud"]
    assert set(cloud) == {PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY}

    measure = cloud[PHASE_CLOUD_MEASURE]
    assert measure["geometry_locked"] is True
    assert measure["thin_evidence"] is False
    # Computed straight from the geometry verdict (SF-1 review finding,
    # 2026-07-27), not read out of the pipeline's own copy — the fixture
    # above deliberately carries no ``pipeline.geometry_guidance`` key to
    # prove that.
    assert measure["geometry_guidance"] == (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )
    assert measure["overall_passed"] is False
    assert measure["excluded_interval_count"] == 2
    # Per-band ``max_deviation_db``/``tolerance_db`` ride along
    # (flat-linearization PR-5 N-3 / PR-7): `/state` is what a chart reads,
    # and per-band numbers missing from the only projection a page sees is
    # the pressure that grows a second derivation downstream.
    #
    # ``max_deviation_hz`` and the GRADED edges ride along for the same
    # reason: a dB with no frequency names no defect to fix, and the top
    # band's graded edge no longer equals its nominal one -- a row printing
    # only ``f_hi_hz`` here would say 16 kHz about a band graded to 20.
    assert measure["spec_bands"] == [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True,
         "graded_lo_hz": 357.14, "graded_hi_hz": 2000.0,
         "max_deviation_db": 1.02, "max_deviation_hz": 412.0,
         "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "passed": True,
         "graded_lo_hz": 2000.0, "graded_hi_hz": 8000.0,
         "max_deviation_db": -1.41, "max_deviation_hz": 5100.0,
         "tolerance_db": 2.0},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False,
         "graded_lo_hz": 8000.0, "graded_hi_hz": 20000.0,
         "max_deviation_db": -4.85, "max_deviation_hz": 11480.0,
         "tolerance_db": 2.5},
    ]
    # PR-7: the report-level reference the tolerance corridor is centered on
    # rides the entry too, copied verbatim like everything else here.
    assert measure["reference_db"] == -27.27
    # The gauge is copied verbatim; the clamp is separable from interference
    # on this live surface (PR-5 SF-2), so a reader can tell a combed room
    # apart from one capture's collapsed gate.
    assert measure["flatness"]["max_db"] == -4.85
    assert measure["flatness"]["rms_db"] == 1.37
    assert measure["validity_floor_hz"] == 187.5

    # A group whose pipeline never became available (combine_failed) reports
    # the honest "nothing to disclose" shape, never a fabricated pass --
    # excluded_interval_count is None, not 0 (SF-1 review finding,
    # 2026-07-27): 0 would read as "the pipeline looked and found nothing",
    # a fabricated-clean claim for a pipeline that never ran.
    verify = cloud[PHASE_CLOUD_VERIFY]
    assert verify["geometry_locked"] is False
    assert verify["overall_passed"] is None
    assert verify["excluded_interval_count"] is None
    assert verify["spec_bands"] == []
    assert verify["geometry_guidance"] == ""
    # Same rule for the two PR-5 keys: unavailable means unknown, never a
    # fabricated zero or a floor of 0 Hz.
    assert verify["flatness"] is None
    # PR-7: same rule again for the chart's own reference level.
    assert verify["reference_db"] is None
    assert verify["validity_floor_hz"] is None


def test_state_cloud_reference_db_survives_an_unbounded_json_integer():
    """#2245: JSON integers are unbounded and ``json`` round-trips one
    happily (a hand-edited or hostile durable state file), but ``float()``
    on one that large RAISES ``OverflowError`` rather than returning
    ``inf`` — on the wizard's poll path, where an escaping conversion is a
    500 on a plain page load. The same hazard
    :func:`_household_findings_status` already guards (the ``10 ** 400``
    case in ``test_an_unusable_clock_becomes_none_and_never_takes_the_row_with_it``
    below); ``_finite`` — read here through ``spec.reference_db``, the
    exact path PR #2242's review found it unreachable-but-real on — now
    catches it too.
    """
    v2host.save_v2_state({
        "session_id": "cap_overflow",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
                "positions": [],
                "pipeline": {
                    "available": True,
                    "merged_excluded_bands_hz": [],
                    "spec": {
                        "overall_passed": True,
                        "reference_db": 10 ** 400,
                        "bands": [],
                    },
                },
            },
        },
    })

    measure = v2status.crossover_v2_status_block()["cloud"][PHASE_CLOUD_MEASURE]
    assert measure["reference_db"] is None
    assert measure["overall_passed"] is True


def test_state_cloud_block_reports_locked_guidance_even_when_pipeline_never_ran():
    """SF-1 review finding (2026-07-27): a locked group's "spread the mic
    further" guidance must survive an unrelated downstream pipeline failure,
    not disappear with it -- geometry locking is decided and RECORDED
    BEFORE the honest-instrument pipeline ever runs (see
    ``_close_cloud_group``), so the guidance is a pure function of the
    geometry verdict alone. Before the fix, an unavailable pipeline
    defaulted ``geometry_guidance`` to ``""`` regardless of the geometry
    verdict -- a locked-but-pipeline-failed group silently lost its one
    actionable piece of copy. Also pins the sibling fix: ``excluded_interval_count``
    is ``None``, never a fabricated ``0``, when the pipeline never became
    available."""
    v2host.save_v2_state({
        "session_id": "cap_state_locked_unavailable",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {
                    "locked": True, "reason": "geometry_locked",
                    "thin_evidence": False,
                },
                "positions": [],
                "pipeline": {"available": False, "reason": "combine_failed"},
            },
        },
    })

    measure = v2status.crossover_v2_status_block()["cloud"][PHASE_CLOUD_MEASURE]
    assert measure["geometry_locked"] is True
    assert measure["excluded_interval_count"] is None
    assert measure["overall_passed"] is None
    assert measure["spec_bands"] == []
    assert measure["geometry_guidance"] == (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


def test_state_cloud_block_is_none_before_any_group_closes():
    v2host.save_v2_state({"session_id": "cap_fresh"})
    assert v2status.crossover_v2_status_block()["cloud"] is None


def test_cloud_summary_stamps_the_producing_session_id():
    """PR-7's provenance marker: ``_cloud_summary`` stamps each closed
    phase's dict with the CONDUCTOR's own session id, so a later carry-
    forward (``persist_conductor_state``'s B1 branch, which copies this
    whole per-phase dict verbatim) can still say which session actually
    produced it — see ``_compact_cloud_status``'s ``provenance_note``."""
    fake = SimpleNamespace(
        session_id="cap_producer_session",
        session_phases=(PHASE_CLOUD_MEASURE,),
        group_geometry=lambda phase: {"locked": True, "reason": "geometry_locked"},
        group_position_takes=lambda phase: [],
        group_cloud_result=lambda phase: {
            "available": True, "spec": {"overall_passed": True},
        },
    )
    summary = v2host._cloud_summary(fake)
    assert summary[PHASE_CLOUD_MEASURE]["session_id"] == "cap_producer_session"


def test_provenance_note_reflects_whether_the_group_matches_the_active_session():
    """The household-facing half of the same marker
    (``_compact_cloud_status``'s ``provenance_note``, PR-7). Three states,
    told apart rather than collapsed: the stamped producer matches the
    caller's current session (nothing to say — the chart is fresh); it
    disagrees (a group carried forward from an earlier session — say so);
    or there is no stamp at all (a durable state written before this marker
    existed — unknown, not stale, so an upgrade cannot manufacture a false
    warning for data nobody ever mis-attributed)."""
    pipeline = {"available": True, "spec": {"overall_passed": True, "bands": []}}
    stamped_state = {
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False},
            "positions": [],
            "pipeline": pipeline,
            "session_id": "cap_producer_session",
        },
    }

    fresh = v2status._compact_cloud_status(
        stamped_state, current_session_id="cap_producer_session",
    )
    assert fresh[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    stale = v2status._compact_cloud_status(
        stamped_state, current_session_id="cap_rearm_session",
    )
    assert stale[PHASE_CLOUD_VERIFY]["provenance_note"] == (
        "This chart is from a previous session's measurement — "
        "re-measure to see this session's own result."
    )

    legacy_state = {
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False}, "positions": [], "pipeline": pipeline,
        },
    }
    legacy = v2status._compact_cloud_status(
        legacy_state, current_session_id="cap_rearm_session",
    )
    assert legacy[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    # Backward compatibility: an existing caller that never passes
    # current_session_id at all (every test seam before this PR) still gets
    # the honest "unknown" reading, not a crash or a fabricated verdict.
    no_current = v2status._compact_cloud_status(stamped_state)
    assert no_current[PHASE_CLOUD_VERIFY]["provenance_note"] == ""


def test_verify_rearm_preserves_candidate_identity_and_cloud_block(monkeypatch):
    """A new VERIFY capture keeps the applied candidate and its cloud evidence.

    B1 (blocker, 2026-07-26 review): a verify-only re-arm's conductor
    (the re-arm's ``index_phase_map={1: PHASE_VERIFY}``) has no
    group phase in ITS OWN session, so ``_cloud_summary`` honestly returns
    ``None`` for it — but the OLD session-id-gated carry-forward turned that
    ``None`` into a destructive overwrite of a real prior cloud verdict.
    One tap of "Try again" (the PRIMARY next_action after a failed verify)
    used to blank `/state.crossover_v2.cloud`, the envelope's ``cloud`` key,
    AND make the doctor report "no cloud-measurement session recorded yet"
    for a session that very much ran.

    Walks: a completed cloud session (durable state seeded, mirroring what
    ``persist_conductor_state`` would have written) -> the REAL re-arm
    conductor + the REAL ``persist_conductor_state`` call (the exact
    production seam the verify-only prepare's ``_open`` uses, mirroring
    ``test_second_apply_way_back_pointer_survives_the_deferred_verify_rearm``'s
    own pattern for the way-back pointer) -> asserts all three surfaces
    (`/state`, the envelope, the doctor) still see the cloud verdict. The
    candidate assertion also pins #2079's crash/retry write identity: the
    fingerprint must survive this same new-session rebind so a recovery
    VERIFY cannot become a second model-error observation.
    """
    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
    from jasper.cli.doctor.correction import check_crossover_v2_cloud_pipeline

    cloud_block = {
        PHASE_CLOUD_MEASURE: {
            "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
            "positions": [{"position_id": "cloud_measure_09", "index": 9, "attempt": 9}],
            "pipeline": {
                "available": True,
                "geometry_guidance": "Spread the mic further.",
                "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                "spec": {
                    "overall_passed": False,
                    "bands": [{"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "passed": False}],
                },
            },
        },
    }
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-original"},
        "applied": True,
        "cloud": cloud_block,
        "evidence": {
            "bundle_session_id": "bundle-1",
            "cloud_artifacts": {PHASE_CLOUD_MEASURE: "artifact-fingerprint-abc"},
        },
    })

    # The real production seam: the verify-only prepare's _open mints a
    # conductor bound to a NEW capture session id and immediately persists it
    # ("Keep the durable candidate/applied facts; rebind the session id.").
    conductor = CrossoverV2Session(
        session_id="cap_rearm_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map={1: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(
        conductor, failure_code=None, evidence={"bundle_session_id": "bundle-2"},
    )

    # Surface 1: the durable state itself.
    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["candidate"] == {"fingerprint": "fp-original"}
    assert state["cloud"] == cloud_block
    assert state["evidence"]["cloud_artifacts"] == {
        PHASE_CLOUD_MEASURE: "artifact-fingerprint-abc"
    }

    # Surface 2: /state's compact projection.
    compact = v2status.crossover_v2_status_block()["cloud"]
    assert compact is not None
    assert compact[PHASE_CLOUD_MEASURE]["geometry_locked"] is True
    assert compact[PHASE_CLOUD_MEASURE]["overall_passed"] is False

    # Surface 3: the envelope.
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    status = {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    }
    envelope = build_crossover_envelope_v2(status)
    assert envelope["cloud"] is not None
    assert envelope["cloud"][PHASE_CLOUD_MEASURE]["geometry_locked"] is True

    # Surface 4 (named "all three" in the review, the doctor makes four):
    # the doctor no longer reports "no cloud-measurement session recorded".
    monkeypatch.setattr(v2host, "load_v2_state", lambda: state)
    r = check_crossover_v2_cloud_pipeline()
    assert "no cloud-measurement session" not in r.detail
    assert f"{PHASE_CLOUD_MEASURE}: spec=fail" in r.detail


def test_a_session_with_its_own_group_phase_overwrites_stale_prior_cloud():
    """N5 review finding (2026-07-27): the B1 fix's guard is "carry ``cloud``
    forward ONLY when THIS conductor's own session has no group phase" — the
    inverse must also hold, and nothing asserted it before this test (a
    regression to an unconditional carry-forward would have gone green).

    A conductor whose OWN session DOES include a group phase (a fresh,
    full — not verify-only — session that has started walking a cloud but
    has not closed any group of its OWN yet) must report ``cloud`` as
    honestly ``None`` for THIS session, never silently inheriting a stale
    verdict from whatever the previous session left behind.
    """
    v2host.save_v2_state({
        "session_id": "cap_stale_prior_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-stale"},
        "applied": True,
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked"},
                "positions": [
                    {"position_id": "cloud_measure_09", "index": 9, "attempt": 9}
                ],
                "pipeline": {"available": True, "spec": {"overall_passed": False}},
            },
        },
    })

    # A NEW full session whose own index_phase_map includes a cloud group
    # phase — mirrors the B1 test's verify-only conductor, but with
    # PHASE_CLOUD_MEASURE instead of PHASE_VERIFY, so this session's
    # session_phases DOES overlap GROUP_PHASES. It has not walked far enough
    # to close that group yet.
    conductor = CrossoverV2Session(
        session_id="cap_fresh_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(),
        applied=False,
        index_phase_map={1: PHASE_CLOUD_MEASURE},
    )
    v2host.persist_conductor_state(conductor, failure_code=None, evidence=None)

    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_fresh_session"
    # Honestly None -- "this session has not closed a group yet" -- never
    # the previous session's stale verdict.
    assert state["cloud"] is None
    assert v2status.crossover_v2_status_block()["cloud"] is None


def _seeded_session_with_a_banked_finding(copy: str) -> None:
    """A completed measuring session whose fit banked one household finding."""
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "evidence": {
            "bundle_session_id": "bundle-stage-1",
            v2host.FINDING_HOUSEHOLD_REFS_KEY: [
                {"household_copy": copy, "at": time.time()},
            ],
        },
    })


def _rearm_conductor(session_id: str, *, index_phase_map: dict) -> Any:
    return CrossoverV2Session(
        session_id=session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map=index_phase_map,
    )


def test_a_persisted_state_write_drops_the_retired_fc_selection():
    """``fc_selection`` is versioned-ABSENT, not versioned-null (ticket 2.4).

    Stage 2 used to copy a measuring session's Fc recommendation forward across
    the seam, because a stage-2 conductor never had one and would otherwise
    persist ``None`` over a live recommendation the household was mid-decision
    on. The selector that produced recommendations is retired, so there is no
    live value to protect and no product read path that reads one (the offline
    archaeology scripts still do, deliberately): the carry-forward
    went with it.

    What replaces it is the honest shape. A persist writes no ``fc_selection``
    key at ALL — not the key set to ``None``, which would read as "a comparison
    that produced nothing", and not a copy of a legacy value, which would carry
    a retired verdict into a record whose version has no such field. A round
    banked under the old build keeps its payload right up until the next write
    ages it out, and no reader touches it either way
    (``test_a_legacy_fc_selection_is_inert_and_never_refuses``).
    """
    legacy = {
        "verdict": "recommend_alternative", "configured_hz": 2000.0,
        "recommended_hz": 1750.0, "margin_db": 1.4, "evaluated": 6,
        "planned": 6, "limits": {}, "refusals": [], "scores": [],
    }
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "fc_selection": legacy,
    })

    v2host.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
    )
    assert "fc_selection" not in (v2host.load_v2_state() or {})

    # The same on the measuring side of the seam — no route writes the key.
    v2host.persist_conductor_state(
        _rearm_conductor(
            "cap_fresh_measure",
            index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE},
        ),
        failure_code=None,
    )
    assert "fc_selection" not in (v2host.load_v2_state() or {})


# --- what the MEASURING session disclosed, across the stage-2 bundle hop -----
#
# The verify-only prepare opens a NEW capture session AND a NEW evidence bundle,
# so stage 2 runs under a conductor that never ran MEASURE and whose own
# persist writes ``None`` over everything the measuring session banked. Without
# the carry-forward the household reads the caveat on the screen where they
# DECIDE and then not on the screen that tells them the speaker is tuned — the
# worse half to lose, because that screen otherwise says only "Verified."
# (CC1; #2087's ripple reservation; audit gauntlet 5a's mic calibration.)

_FINDING_COPY = "Two measurements of how this speaker's ranges balance disagreed."
_RIPPLE_RESERVATION = {"predicted_ripple_db": 15.244, "threshold_db": 15.0}


def _seeded_session_with_a_reservation(measure: dict) -> None:
    """A completed measuring session whose accepted MEASURE banked one."""
    v2host.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "measure": dict(measure),
        "evidence": {"bundle_session_id": "bundle-stage-1"},
    })


_ABSENT = object()


def _dig(payload, path, *, missing=None):
    """Read ``path`` out of a projection, absence included.

    A step that is missing or out of range reads as ``missing``; a step whose
    stored value really is ``None`` reads as ``None``. The two collapse together
    by default, which is what most callers want — but "the key was REMOVED" and
    "the key was written as None" are different clearings, and a caller that
    must tell them apart passes ``missing=_ABSENT``.
    """
    for step in path:
        if payload is None:
            return missing
        try:
            payload = payload[step]
        except (KeyError, IndexError):
            return missing
    return payload


@pytest.mark.parametrize(
    ("seed", "state_path", "status_path", "expected", "screen_key", "copy_key",
     "screen_exact", "expert_detail"),
    (
        pytest.param(
            lambda: _seeded_session_with_a_banked_finding(_FINDING_COPY),
            ("evidence", v2host.FINDING_HOUSEHOLD_REFS_KEY, 0, "household_copy"),
            ("findings", 0, "household_copy"),
            _FINDING_COPY,
            "findings", "finding", True, None,
            id="banked-finding",
        ),
        pytest.param(
            lambda: _seeded_session_with_a_reservation(
                {"ripple_reservation": _RIPPLE_RESERVATION}),
            ("measure", "ripple_reservation"),
            ("measure", "ripple_reservation"),
            _RIPPLE_RESERVATION,
            "nudges", "ripple", False,
            "predicted ripple 15.24 dB, above the 15.0 dB disclosure threshold",
            id="ripple-reservation",
        ),
        pytest.param(
            lambda: _seeded_session_with_a_reservation(
                {"calibration_reservation": True}),
            ("measure", "calibration_reservation"),
            ("measure", "calibration_reservation"),
            True,
            "nudges", "mic_calibration", False, None,
            id="mic-calibration-reservation",
        ),
    ),
)
def test_stage_2_keeps_what_the_measuring_session_disclosed(
    monkeypatch, seed, state_path, status_path, expected, screen_key, copy_key,
    screen_exact, expert_detail,
):
    """Walks the real seam: seeded durable state -> the REAL re-arm conductor
    -> the REAL ``persist_conductor_state`` -> the three surfaces the
    disclosure has to reach (durable state, ``/state``, the done screen).
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        MIC_CALIBRATION_RESERVATION_COPY,
        RIPPLE_RESERVATION_COPY,
        build_crossover_envelope_v2,
    )

    seed()
    v2host.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-stage-2"},
    )

    # Surface 1: the durable state — carried across the bundle hop.
    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["evidence"]["bundle_session_id"] == "bundle-stage-2"
    assert _dig(state, state_path) == expected

    # Surface 2: /state's projection.
    status = v2status.crossover_v2_status_block()
    assert _dig(status, status_path) == expected

    # Surface 3: the screen the household actually reads.
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            **status, "phase": PHASE_DONE, "verify": {"outcome": "pass"},
        },
    })
    copy = {
        "finding": _FINDING_COPY,
        "ripple": RIPPLE_RESERVATION_COPY,
        "mic_calibration": MIC_CALIBRATION_RESERVATION_COPY,
    }[copy_key]
    texts = [row["text"] for row in env[screen_key]]
    assert texts == [copy] if screen_exact else copy in texts
    if expert_detail is not None:
        assert expert_detail in env["expert_details"]


# ``cleared_state`` is a row parameter because the two rows are cleared in
# DIFFERENT ways, and flattening them to a single `is None` would let a
# regression that wrote None over the removed key pass the finding row.
@pytest.mark.parametrize(
    ("seed", "state_path", "cleared_state", "status_path", "cleared_status"),
    (
        pytest.param(
            lambda: _seeded_session_with_a_banked_finding(
                "An old finding nobody re-measured."),
            ("evidence", v2host.FINDING_HOUSEHOLD_REFS_KEY),
            # REMOVED from the evidence map, not written as None.
            _ABSENT,
            ("findings",),
            [],
            id="banked-finding",
        ),
        pytest.param(
            lambda: _seeded_session_with_a_reservation(
                {"ripple_reservation": _RIPPLE_RESERVATION}),
            ("measure",),
            # Still there, holding None: the key is the whole measure block.
            None,
            ("measure",),
            None,
            id="ripple-reservation",
        ),
    ),
)
def test_a_fresh_measurement_clears_what_the_previous_session_disclosed(
    seed, state_path, cleared_state, status_path, cleared_status,
):
    """The converse, and the reason the predicate is MEASURE rather than an
    unconditional carry: a new measuring session owns the answer to "what did
    this measurement learn", so a clean retake must not replay a caveat about a
    capture the household already replaced.
    """
    seed()

    # A fresh full session: its own session_phases include MEASURE.
    v2host.persist_conductor_state(
        _rearm_conductor("cap_fresh_session", index_phase_map={1: PHASE_MEASURE}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-fresh"},
    )

    assert _dig(v2host.load_v2_state(), state_path, missing=_ABSENT) is cleared_state
    assert _dig(v2status.crossover_v2_status_block(), status_path) == cleared_status


# --- the projection contract, pinned AT the projection layer ------------------
#
# Gate finding SF-1 (adversarial review of #1982): `_household_findings_status`
# docstrings its whole contract — "a row without usable copy is DROPPED … an
# unusable `at` becomes None. Fabricating neither a sentence nor a date" — and
# nothing asserted it HERE. The gate proved the gap by weakening the copy check
# to `str(row.get("household_copy") or "")`, which renders a fabricated "42" on
# the household done screen end to end with the whole suite green: every
# screen-level test hands the envelope well-formed rows, so none of them can
# see a projection that coerces. These pin the layer that actually decides.


def _plant_unbankable_v2_state(state: Any) -> None:
    """Plant durable state that ``save_v2_state`` itself would REFUSE.

    Since #2839 the writer passes ``allow_nan=False``, so it can no longer
    produce a state file carrying a non-finite number. A file written by a
    build that predates that guard still can, and ``json.loads`` accepts the
    bare ``NaN`` / ``Infinity`` literals on the way back in — so the FILE, not
    the writer, is the surface the reader guards below defend, exactly as
    ``10 ** 400`` is (JSON integers are unbounded and no writer produces one
    either). Written the way that build would have: the envelope through the
    real writer, the value it now refuses spliced in after.
    """
    v2host.save_v2_state({"session_id": "cap_placeholder"})
    path = Path(v2host._state_path())
    envelope = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps({**envelope, **state}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _findings_state(rows: Any) -> None:
    """Durable state whose projection is exactly ``rows``.

    Planted as a file rather than through ``save_v2_state``: the rows here are
    hostile by construction, and some of them are values the writer refuses
    since #2839 — see :func:`_plant_unbankable_v2_state`. The subject of these
    tests is the projection layer, not the writer.
    """
    _plant_unbankable_v2_state({
        "session_id": "cap_projection",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
        "evidence": {
            "bundle_session_id": "bundle-1",
            v2host.FINDING_HOUSEHOLD_REFS_KEY: rows,
        },
    })


@pytest.mark.parametrize("copy", [
    42,                     # the gate's own mutation subject — `str(42)` = "42"
    42.5,
    True,                   # bool is an int; `str(True)` = "True"
    None,                   # `str(None or "")` = "" — falsy, but still not text
    ["a sentence"],
    {"text": "a sentence"},
    "",
    "   ",
    "\n\t ",
])
def test_a_row_without_a_real_sentence_is_dropped_never_coerced(copy):
    """**A finding is prose or it is nothing.** Never `str()`-ed into existence.

    The failure this forbids is not cosmetic: a coerced row puts a fabricated
    sentence — "42", "True", "None" — into the one register this program
    promises is household-readable, on the screen that tells someone their
    speaker is tuned. Dropping is the honest answer to a row this build cannot
    read, exactly as `read_finding_set` returning None is the honest answer to
    a bundle that never banked one.
    """
    _findings_state([{"household_copy": copy, "at": time.time()}])
    assert v2status.crossover_v2_status_block()["findings"] == []


def test_a_good_row_survives_beside_every_unusable_one():
    """Guards the guard: the drops above are a FILTER, not this layer refusing
    to project at all. A test suite where every projection came back empty
    would pass the assertions above while shipping nothing."""
    _findings_state([
        {"household_copy": 42, "at": time.time()},
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
        "not even an object",
        {"household_copy": "", "at": time.time()},
    ])
    assert v2status.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
    ]


@pytest.mark.parametrize("at", [
    None,
    "2026-07-29T10:00:00Z",   # an ISO string is not this file's clock
    True,                     # bool is an int, and it is not a timestamp
    [1_700_000_000.0],
    float("nan"),
    float("inf"),
    float("-inf"),
    10 ** 400,                # nit 1: `float()` RAISES OverflowError here
])
def test_an_unusable_clock_becomes_none_and_never_takes_the_row_with_it(at):
    """**The date is dropped; the sentence is not.** An unreadable ``at`` means
    "we cannot say when", which the envelope renders as "From your measurement
    earlier: …" — a real disclosure with no date CLAIM. Losing the whole finding
    over a bad byte in its timestamp would trade a missing date for a missing
    diagnosis.

    ``10 ** 400`` is the nit-1 case and it is reachable through the file, not
    theoretical: JSON integers are unbounded, `json` round-trips one happily,
    and `float()` on it RAISES `OverflowError` rather than returning `inf` — on
    the wizard's 1.5 s poll path, where an escape is a 500 on a plain page load.
    """
    _findings_state([{"household_copy": "A real one.", "at": at}])
    assert v2status.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": None},
    ]


def test_the_projection_reads_only_its_two_fields():
    """A durable row written by a later build — one that persists the mechanism
    beside the copy — must not leak that field onto `/state`. The reader NAMES
    what it takes rather than passing a row through, so a field added upstream
    cannot publish itself here."""
    _findings_state([{
        "household_copy": "A real one.",
        "at": 1_700_000_000.0,
        "mechanism": "M7",
        "evidence": {"disagreement_db": 3.2307},
    }])
    assert v2status.crossover_v2_status_block()["findings"] == [
        {"household_copy": "A real one.", "at": 1_700_000_000.0},
    ]


@pytest.mark.parametrize("rows", [None, {}, "findings", 7, [None, 5, "x"]])
def test_a_malformed_projection_block_reads_as_no_findings(rows):
    """A whole projection key that is not a list of objects is "nothing banked",
    never a crash on the poll path."""
    _findings_state(rows)
    assert v2status.crossover_v2_status_block()["findings"] == []


def test_a_corrupt_session_phases_list_never_reads_as_done():
    """S5: ``session_phases`` filters to the empty tuple on garbage, and a
    zero-length walk falls through to PHASE_DONE — i.e. a garbled state file
    would tell a household "Your speaker is tuned". Fail toward the fallback
    instead."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_MEASURE

    # A partially-recognisable list keeps only what it can name — and that IS
    # enough to walk, so it is used rather than discarded.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": ["nonsense", PHASE_VERIFY],
        "applied": True,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_VERIFY


# --- the stage-1 PHASE_DONE collision (two-stage commission PR-T2) ------------


def test_a_measure_only_session_resolves_to_review_never_done():
    """**The work order's premise 6, and PR-T2's first pin.**

    ``_phase_from_state`` walks the recorded ``session_phases`` and returns
    PHASE_DONE once each is accepted. Its one special case — VERIFY unaccepted
    with MEASURE accepted and not applied ⇒ PHASE_APPLYING — cannot fire when
    VERIFY is not in the recorded phases at all. So a stage-1 session (CHECK,
    MEASURE, CLOUD_MEASURE, no VERIFY) fell straight through to PHASE_DONE:
    the RESULT screen, whose copy is "Your speaker is tuned", over a speaker
    that had been measured and never touched. A direct collision, not a
    theoretical one — and the acceptance criterion is explicit that "a stage-1
    session never renders 'your speaker is tuned'".
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_REVIEW


def test_an_applied_measure_only_session_resolves_to_verify_not_review_or_done():
    """RE-DERIVED from PR-T2's ``…_still_resolves_to_done`` (work order D2).

    T2 pinned that ``applied`` wins over the review branch, which is still
    true and still the point: re-offering "apply this?" over a speaker that
    already has it would be the mirror of the bug T2 fixed. What T2 could not
    yet express is where an applied measure-only session goes INSTEAD, because
    stage 2 did not exist — so it pinned ``done``, the only other terminal.

    T3 makes that answer wrong: stage 1 measured, the household applied from
    the review screen, and the post-apply check has not been opened yet. "Your
    speaker is tuned" over an unverified correction is exactly the class of
    claim this work order exists to remove. The honest resolution is
    PHASE_VERIFY, whose screen carries the action that opens stage 2.
    """
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": True,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_VERIFY
    # …and the review interlude is NOT re-offered.
    assert v2status.crossover_v2_status_block()["phase"] != "review"

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    })
    assert env["screen"] == "verify"
    # The stage-2 entry point, tier-matched by the durable state's own tier.
    assert env["next_action"]["endpoint"] == "/correction/crossover/v2/verify"
    assert env["next_action"]["body"] == {"stage": "post_apply"}


def test_a_session_that_verified_still_resolves_to_done():
    """The review branch keys on a session that never intended to VERIFY, so
    every shape that DID keeps its shipped terminal — a full pre-cloud session
    and a verify-only re-arm alike. Without this the fix would silently move
    the RESULT screen for the flows that already work."""
    for phases in (
        [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        [PHASE_VERIFY],
        [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE, PHASE_VERIFY,
         PHASE_CLOUD_VERIFY],
    ):
        v2host.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": list(phases),
            "session_phases": list(phases),
            "applied": True,
        })
        assert v2status.crossover_v2_status_block()["phase"] == PHASE_DONE, phases


def test_a_corrupt_state_cannot_reach_the_review_screen_either():
    """The corrupt-state fallback the walk already documents must keep working
    — and must not become a NEW way to reach the review screen.

    A garbled ``session_phases`` filters to the empty tuple and walks
    PRE_CLOUD_CAPTURE_PHASES, which DOES contain VERIFY, so it can never
    satisfy the measure-only test on the strength of an unreadable state file.
    It resolves through the loop to its first unaccepted phase, exactly as
    before this change.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    phase = v2status.crossover_v2_status_block()["phase"]
    assert phase != PHASE_REVIEW
    # MEASURE is accepted and VERIFY is not, so the shipped special case owns
    # this state — the honest "the apply is in flight" answer, not a terminal.
    assert phase == PHASE_APPLYING


# --- the stage-2 openability preflight (D3, render-time half) ----------------


def _preflight_status(phase, **v2):
    # A candidate by default: the preflight also gates on one (PR-T3), because
    # with nothing to apply there is nothing to preflight, and every REAL
    # review screen that renders an Apply control has one. A fixture without
    # it would be testing the cost gate, not the predicate — the tests that
    # ARE about the cost gate pass ``candidate=None`` explicitly.
    v2.setdefault("candidate", {"fingerprint": "fp-preflight"})
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": phase, **v2},
    }


def test_the_preflight_runs_only_on_the_review_screen():
    """It is the review screen's own honesty layer, and it is not free: one
    call is several JSON loads, a profile fingerprint, and a preset compile,
    and ``ensure_crossover_preview_ready()`` can WRITE a regenerated preview.
    No other screen may pay that, so the phase gate is a contract, not an
    optimisation — pinned by refusing to let a non-review phase call the
    predicate at all. (``closing`` is in the list for PR-T3's own reason: it
    is a POLLED screen — the capture is still live — so the predicate running
    there would be the 1.5 s write loop the cost paragraph names.)"""
    calls = []

    def _boom(status):
        calls.append(status)
        raise AssertionError("the preflight must not run off the review screen")

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _boom
    try:
        for phase in (PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE,
                      PHASE_VERIFY, "applying", "closing", "done"):
            status = _preflight_status(phase)
            v2host.attach_stage2_preflight(status)
            assert v2host.STAGE2_PREFLIGHT_KEY not in status["crossover_v2"]
        assert calls == []
    finally:
        v2host.resolve_conductor_context = original


def test_a_refused_preflight_carries_the_predicates_own_sentence(caplog):
    """The refusal messages already name what to finish first, so they are
    passed through verbatim rather than re-phrased here — and the refusal gets
    a named log line, because a household-visible dead end nobody can grep for
    is not a disclosure."""
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    def _refuse(status):
        raise v2host.CrossoverV2Refused(
            "protected speaker setup is not ready; finish it before measuring",
        )

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _refuse
    try:
        status = _preflight_status(PHASE_REVIEW)
        with caplog.at_level(logging.WARNING):
            v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    preflight = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]
    assert preflight["ok"] is False
    assert preflight["message"] == (
        "protected speaker setup is not ready; finish it before measuring"
    )
    assert "event=correction.crossover_v2_stage2_preflight_refused" in caplog.text


def test_a_coded_refusal_carries_its_registrys_own_resolution_control():
    """#1820's precedent: a refusal that knows the exact control which clears
    it declares that control, from the SAME registry entry the hard-stop screen
    reads — so the review screen's message and its button can never disagree
    about what the household should do next."""
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    def _refuse(status):
        raise v2host.CrossoverV2Refused(
            "safety limits are not confirmed",
            code="program_profile_not_confirmed",
        )

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _refuse
    try:
        status = _preflight_status(PHASE_REVIEW)
        v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    action = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["next_action"]
    assert action and action["id"] == "review_safety_limits"


def test_an_unexpected_preflight_failure_fails_closed(caplog):
    """"We could not check" and "we checked and it is fine" must never render
    as the same screen. An unexpected exception writes a not-ok disclosure on
    its own honest sentence — the Apply control no longer keys on it, but a
    screen that renders quiet over a check that never ran would be fabricating
    a clean reading."""
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    def _explode(status):
        raise OSError("the topology file is unreadable")

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = _explode
    try:
        status = _preflight_status(PHASE_REVIEW)
        with caplog.at_level(logging.WARNING):
            v2host.attach_stage2_preflight(status)
    finally:
        v2host.resolve_conductor_context = original

    preflight = status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]
    assert preflight["ok"] is False
    assert "could not check" in preflight["message"]
    assert "event=correction.crossover_v2_stage2_preflight_refused" in caplog.text


def test_a_session_that_ended_with_nothing_still_reaches_the_review_screen():
    """The state ``closing`` must NOT swallow the absence case.

    **What this actually covers, stated precisely:** durable state with every
    stage-1 phase accepted, no candidate, and NO ``cloud_close`` — which is
    state written before this field existed, or state whose ``cloud_close``
    was never populated. It is genuine and correctly handled: the review
    screen's absence copy plus "measure again" is the honest answer for a
    session that is not in progress. It is NOT a live-conductor path — no live
    conductor reaches all-phases-accepted with an empty ``cloud_close``,
    because accepting the group's last index stashes the combine and the
    property reads ``awaiting_confirm`` from that moment until a candidate
    exists. The pin is about the READER's fallback, not about a state the
    writer can produce."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": False,
    })
    block = v2status.crossover_v2_status_block()
    assert block["phase"] == "review"
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })
    assert env["screen"] == "review"
    assert any(a["id"] == "review_remeasure" for a in env["alternate_actions"])


def test_the_preflight_does_not_run_without_a_candidate(caplog):
    """The cost gate (gate blocker B2's second half). The preflight is not
    cheap — six JSON reads, a profile fingerprint, a preset compile, and
    ``ensure_crossover_preview_ready`` which can WRITE two files and logs on
    every call — and it used to run on every 1.5 s poll of a live held window
    (~80 calls per hold, measured). With no candidate there is nothing to
    apply, so there is nothing to preflight."""
    calls: list = []
    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: calls.append(status) or object()
    try:
        status = {
            "active": True,
            "crossover_v2": {"phase": "review", "candidate": None},
        }
        v2host.attach_stage2_preflight(status)
        assert calls == []
        assert v2host.STAGE2_PREFLIGHT_KEY not in status["crossover_v2"]
        # …and a candidate WITH a fingerprint still runs it, so the gate is a
        # condition rather than a switch that turned the feature off.
        status["crossover_v2"]["candidate"] = {"fingerprint": "abc"}
        v2host.attach_stage2_preflight(status)
        assert len(calls) == 1
        assert status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["ok"] is True
    finally:
        v2host.resolve_conductor_context = original


def test_a_stage_1_map_has_no_verify_and_a_stage_2_map_does():
    """**The deliberate T3 tripwire, re-derived** (work order D1/D2).

    PR-T2 pinned ``test_every_shipped_index_phase_map_contains_verify``: every
    shipped ``index_phase_map`` contained a VERIFY, which was the load-bearing
    half of its claim that ``PHASE_REVIEW`` — and therefore
    ``attach_stage2_preflight`` — cost nothing, because the review branch keys
    on VERIFY's absence and nothing could produce it. T2 wrote that pin
    expecting T3 to break it, and named the break as the moment to re-read the
    preflight's cost paragraph.

    **The new invariant, stated explicitly:** a STAGE-1 (measuring) map
    contains no VERIFY entry, by design — its absence is what resolves a
    measure-only session to the review interlude instead of "your speaker is
    tuned". A STAGE-2 (post-apply) map always contains exactly one, at index 1,
    because a post-apply session that verified nothing would have nothing to
    grade. Both halves are checked across both tiers and all four corners of
    Full's validated (N, M) box, so neither rests on the default counts.

    **The cost paragraph, re-read.** ``attach_stage2_preflight`` is now
    genuinely reachable, once per envelope GET while the review interlude is
    on screen. T2 named the one shape it would not survive: a review screen
    rendering beside a permanently in-flight capture, which would turn
    ``ensure_crossover_preview_ready``'s writes into a 1.5 s loop. T3 owns
    re-checking that, and it holds — the review screen is reached only AFTER
    stage 1's session has ended (its runner returns, the capture is purged, and
    the wizard's poll stops at ``captureIsActive(env.capture)``), and the interlude
    itself starts no session. The calls are bounded to the seconds a
    just-closed capture spends winding down, exactly as T2 predicted.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        DEFAULT_CLOUD_VERIFY_POSITIONS,
        MAX_CLOUD_MEASURE_POSITIONS,
        MIN_CLOUD_MEASURE_POSITIONS,
        MIN_CLOUD_VERIFY_POSITIONS,
        TIER_EXPRESS,
        TIER_FULL,
        build_v2_verify_index_phase_map,
    )

    for tier in (TIER_FULL, TIER_EXPRESS):
        stage1 = build_v2_cloud_index_phase_map(tier=tier)
        assert PHASE_VERIFY not in stage1.values(), tier
        assert PHASE_CLOUD_VERIFY not in stage1.values(), tier
        stage2 = build_v2_verify_index_phase_map(
            plan_shape=resolve_plan_shape(tier)
        )
        assert stage2[1] == PHASE_VERIFY, tier
        assert sum(1 for p in stage2.values() if p == PHASE_VERIFY) == 1, tier
    # The corners of the configurable (N, M) space plus the shipped default.
    # N is DERIVED from its own two bounds rather than written out: the upper
    # one moved (12 -> 11) when #2291's entry baseline took a capture blob index,
    # and a literal here would have made this test fail for the wrong reason
    # instead of following the constant it is exercising the extremes of.
    #
    # M's LOW corner follows the same rule and for the same reason: it moved
    # (5 -> 6) when the 2026-08-24 geometry ruling gave the post-apply group its
    # own pose set, and a literal would have failed this test on the floor
    # rather than on the invariant it is about. The high corner stays a literal
    # — M has no derived ceiling, and 12 is simply well past any shipped shape.
    _n_lo, _n_hi = MIN_CLOUD_MEASURE_POSITIONS, MAX_CLOUD_MEASURE_POSITIONS
    _m_lo = MIN_CLOUD_VERIFY_POSITIONS
    for n, m in (
        (_n_lo, _m_lo), (_n_hi, _m_lo), (_n_lo, 12), (_n_hi, 12),
        (9, DEFAULT_CLOUD_VERIFY_POSITIONS),
    ):
        shape = resolve_plan_shape(
            cloud_measure_positions=n, cloud_verify_positions=m,
        )
        stage1 = build_v2_cloud_index_phase_map(plan_shape=shape)
        assert PHASE_VERIFY not in stage1.values(), (n, m)
        assert PHASE_CLOUD_VERIFY not in stage1.values(), (n, m)
        stage2 = build_v2_verify_index_phase_map(plan_shape=shape)
        assert stage2[1] == PHASE_VERIFY, (n, m)
        assert sum(1 for p in stage2.values() if p == PHASE_VERIFY) == 1, (n, m)
    # The recovery re-verify keeps its shipped one-entry map.
    assert build_v2_verify_index_phase_map() == {1: PHASE_VERIFY}


def test_the_envelope_route_actually_runs_the_preflight():
    """The wiring, pinned at its one call site.

    The disclosure fails CLOSED, so dropping this call does not break loudly —
    every review screen warns with the reader's generic fallback sentence
    forever (absence is not a clean reading), which looks like a product bug
    rather than a missing line. ``handle_envelope`` is the only
    path that serves this envelope to the wizard, so the call belongs there and
    a source read is enough to prove it has not been lost in a refactor (same
    shape as ``test_the_session_preparer_rearms_the_walked_away_volume_ceiling``
    above, and for the same reason).
    """
    import inspect

    from jasper.web import correction_crossover_flow

    source = inspect.getsource(correction_crossover_flow.handle_envelope)
    assert "attach_stage2_preflight(status)" in source
    # ...and BEFORE the envelope is built, or it would stamp a status nobody
    # reads.
    assert source.index("attach_stage2_preflight(status)") < source.index(
        "_build_envelope_logged(status)"
    )


def test_a_resolvable_context_renders_a_quiet_review_screen():
    """The positive case, end to end through the envelope: a preflight that
    resolves writes ``ok: True``, the review screen carries no preflight
    warning, and Apply is offered."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_REVIEW

    original = v2host.resolve_conductor_context
    v2host.resolve_conductor_context = lambda status: object()
    try:
        status = _preflight_status(
            PHASE_REVIEW,
            candidate={"fingerprint": "fp-1", "trims_db": {"woofer": -2.0}},
            prediction={
                "curve": {"freqs_hz": [100.0], "magnitude_db": [80.0]},
                "spec_bands": [], "overall_passed": True, "reference_db": 80.0,
            },
        )
        v2host.attach_stage2_preflight(status)
        assert status["crossover_v2"][v2host.STAGE2_PREFLIGHT_KEY]["ok"] is True
        env = build_crossover_envelope_v2(status)
        assert env["screen"] == "review"
        assert env["next_action"]["enabled"] is True
        assert not [n for n in env["nudges"]
                    if n["code"] == "crossover_v2_stage2_preflight_refused"]
    finally:
        v2host.resolve_conductor_context = original


def test_the_session_preparer_rearms_the_walked_away_volume_ceiling():
    """S4: the ceiling is only correct because the preparer re-arms it from the
    plan the stage it opened actually emitted — the volume plan is
    process-global, so a dropped call silently leaves the previous session's
    ceiling in force. Nothing enforced that; this does, by reading the call out
    of the preparer's source.
    """
    import inspect

    # The derivation is now BOUND to a name (issue #2509 sizes the capture session
    # from the same number), so pinning the two tokens separately would no
    # longer prove the value reaches the arm. Pin the binding and the arm.
    source = inspect.getsource(v2host.prepare_v2_session)
    assert "ceiling_s = session_wall_clock_ceiling_s(spec.capture_plan)" in (
        source
    ), "the preparer must size the ceiling from the plan it emits"
    assert "set_wall_clock_ceiling_s(ceiling_s)" in source, (
        "the preparer must arm the ceiling it derived"
    )
    # And the two plans really do want different ceilings, which is the whole
    # reason the re-arm cannot be done once at import.
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_capture_plan,
        build_v2_verify_capture_plan,
        session_wall_clock_ceiling_s,
    )
    from jasper.active_speaker.session_volume_plan import DEFAULT_WALL_CLOCK_CEILING_S

    assert session_wall_clock_ceiling_s(
        build_v2_capture_plan(_roles(), FC_HZ)
    ) > session_wall_clock_ceiling_s(
        build_v2_verify_capture_plan(FC_HZ)
    ) == DEFAULT_WALL_CLOCK_CEILING_S


# --- the apply's stage-2 openability preflight (work order D3, PR-T3 half) ---


def _ready_to_apply(monkeypatch, tmp_path):
    """The real apply environment plus durable state holding its candidate.

    Same seeding the neighbouring apply tests use, so these exercise the REAL
    ``handle_v2_apply`` up to (and, when the preflight refuses, not past) the
    transaction.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_preflight",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    return candidate


def test_apply_refuses_when_stage_2_could_not_be_opened(monkeypatch, tmp_path):
    """**The pin the work order names for this rung.** A speaker that cannot
    open its post-apply check must not be corrected and left ungraded — the
    applied-and-ungraded end state this whole work order exists to eliminate.

    T2 shipped the render-time half (Apply is disabled and the refusal renders
    verbatim). This is the server-side half, and it is NOT redundant with it: a
    disabled control is not a security boundary — a stale page, a second tab,
    or a direct POST all reach this endpoint.
    """
    candidate = _ready_to_apply(monkeypatch, tmp_path)

    def _refuse(_status):
        raise v2host.CrossoverV2Refused(
            "confirm the driver safety profile before measuring"
        )

    monkeypatch.setattr(v2host, "resolve_conductor_context", _refuse)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )

    # The predicate's OWN sentence reaches the household, not a generic one.
    assert "confirm the driver safety profile" in str(excinfo.value)
    assert "was not run" in str(excinfo.value)
    # The DSP was never touched: nothing durable claims an apply happened,
    # and no way-back pointer was recorded (which only a real commit
    # produces).
    state = v2host.load_v2_state()
    assert state.get("applied") is not True
    assert state.get("previous_candidate_fingerprint") is None


def test_an_unexpected_preflight_failure_refuses_the_apply_too(
    monkeypatch, tmp_path,
):
    """Fail-closed in BOTH directions: "we could not check" and "we checked
    and it is fine" must never produce the same outcome on the one action that
    touches the speaker."""
    candidate = _ready_to_apply(monkeypatch, tmp_path)

    def _explode(_status):
        raise RuntimeError("the topology file is unreadable")

    monkeypatch.setattr(v2host, "resolve_conductor_context", _explode)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )
    assert "could not confirm" in str(excinfo.value)
    assert v2host.load_v2_state().get("applied") is not True


def test_the_preflight_runs_after_the_freshness_gates(monkeypatch):
    """Ordering: a STALE candidate gets its own specific refusal ("review the
    newest measurement"), not the preflight's. Getting this backwards would
    tell a household to go fix their safety profile when what they actually
    need is to re-read a newer measurement."""
    v2host.save_v2_state({
        "session_id": "cap_preflight",
        "candidate": {"fingerprint": "a-newer-fingerprint"},
        "applied": False,
    })
    monkeypatch.setattr(
        v2host, "resolve_conductor_context",
        lambda _status: (_ for _ in ()).throw(
            v2host.CrossoverV2Refused("safety profile")
        ),
    )

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_apply(
            {"expected_candidate_fingerprint": "the-reviewed-one"},
            _bg_run_async,
            _FakeApplyCam,
            status={},
        )
    assert "no longer current" in str(excinfo.value)


def test_the_apply_endpoint_cannot_skip_the_preflight():
    """``status`` is REQUIRED and keyword-only, so no caller can quietly drop
    it — and the dispatch really does supply one."""
    import inspect

    sig = inspect.signature(v2host.handle_v2_apply)
    status_param = sig.parameters["status"]
    assert status_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert status_param.default is inspect.Parameter.empty

    from jasper.web import correction_setup

    source = inspect.getsource(correction_setup._handle_crossover_v2_apply)
    assert "status=correction_crossover_backend.status_payload()" in source
    # …and the call site really is inside handle_v2_apply, before the commit.
    apply_source = inspect.getsource(v2host.handle_v2_apply)
    assert "_assert_stage_2_can_open(status)" in apply_source
    # rindex, not index: the first `apply_baseline_profile(` is the import.
    assert apply_source.index("_assert_stage_2_can_open(status)") < apply_source.rindex(
        "apply_baseline_profile("
    )


# --- stage 2's entry point (work order D2) -----------------------------------


def test_the_verify_endpoint_opens_the_tier_matched_stage_2_or_the_recovery():
    """ONE entry point, two shapes — generalized over the plan shape rather
    than forked into a second builder (work order D2).

    The tier comes from the durable state the MEASURING session wrote, so the
    household's choice at the tier chooser governs both stages.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        DEFAULT_CLOUD_VERIFY_POSITIONS,
        TIER_EXPRESS,
        TIER_FULL,
        build_v2_verify_capture_plan,
    )

    # Full's count is DERIVED, not the literal 6: it moved twice in a week
    # (6 -> 5 on the 2026-08-18 trim, 5 -> 6 when the 2026-08-24 geometry ruling
    # put the design axis into the pose set), and this test is about the tier
    # MATCH rather than about either number.
    for tier, expected in (
        (TIER_FULL, DEFAULT_CLOUD_VERIFY_POSITIONS), (TIER_EXPRESS, 1),
    ):
        shape = v2host._verify_plan_shape({"stage": "post_apply"}, {"tier": tier})
        assert shape == resolve_plan_shape(tier)
        assert build_v2_verify_capture_plan(
            FC_HZ, plan_shape=shape,
        ).capture_target == expected

    # Absent / explicit "recovery" is the shipped 1-entry re-arm, which is what
    # a FAILED stage 2 offers — every pre-two-stage caller posts `{}`.
    for raw in ({}, {"stage": "recovery"}, None):
        assert v2host._verify_plan_shape(raw, {"tier": TIER_FULL}) is None
    assert build_v2_verify_capture_plan(FC_HZ).capture_target == 1


def test_an_unknown_verify_stage_is_refused_rather_than_guessed():
    """Same strictness ``normalize_tier`` applies to a tier: a caller asking
    for an instrument this build does not have fails loudly rather than
    silently measuring something else."""
    from jasper.active_speaker.crossover_v2_flow import TIER_FULL

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host._verify_plan_shape({"stage": "turbo"}, {"tier": TIER_FULL})
    assert "unknown verify stage" in str(excinfo.value)


def test_the_failed_screens_re_verify_still_asks_for_the_recovery():
    """The shipped ``verify_retry`` action posts no ``stage``, so a failed
    post-apply check still offers ONE cheap sweep rather than re-walking the
    whole post-apply cloud. Read off the envelope so a body change is visible
    here rather than on hardware."""
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "phase": PHASE_VERIFY,
            "applied": True,
            # Stamped now: this is the screen a household is on, which is the
            # only state that renders the live verify_fail actions (#1942).
            "failure": {"code": "verify_out_of_tolerance", "at": time.time()},
        },
    })
    retry = env["next_action"]
    assert retry["endpoint"] == "/correction/crossover/v2/verify"
    assert "stage" not in (retry.get("body") or {})


def _rearm_conductor_for_persist(session_id: str, index_phase_map: dict, **kwargs):
    """A conductor of the verify-only prepare's shape, seams stubbed — the same
    construction ``test_verify_rearm_does_not_blank_the_persisted_cloud_block``
    uses to exercise the REAL ``persist_conductor_state``."""
    return CrossoverV2Session(
        session_id=session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map=index_phase_map,
        **kwargs,
    )


def test_verify_rearm_keeps_the_prior_level_reference_across_its_own_writes():
    """#1927: the history the disclosure reads must survive the opening
    persist of a re-arm, which runs BEFORE any usable VERIFY attempt has set
    this session's own reference. Same carry-forward shape as ``tier`` and
    ``cloud`` — a re-arm runs under a brand-new capture session id, so a
    session-id guard would drop it on the first "Try again"."""
    reference = {"values": {"summed": -20.0}, "at": 1_700_000_000.0}
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {"pilot_transfer_reference": reference},
    })
    conductor = _rearm_conductor_for_persist(
        "cap_rearm_session", {1: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["verify_priors"]["pilot_transfer_reference"] == reference


def test_seeding_a_rearm_from_durable_state_never_seeds_the_comparator():
    """The SEEDING path end to end, minus the capture: durable state carrying a
    previous session's reference → the value the verify-only prepare passes as
    ``verify_pilot_transfer_prior`` → a fresh conductor. The comparator stays
    empty; only the history arrives (#1927)."""
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {
            "pilot_transfer_reference": {
                "values": {"summed": -20.0}, "at": time.time() - 86400.0,
            },
        },
    })
    prior = v2host.pilot_transfer_prior_from_state(v2host.load_v2_state())
    assert prior["values"] == {"summed": -20.0}
    conductor = _rearm_conductor_for_persist(
        "cap_rearm_session", {1: PHASE_VERIFY},
        verify_pilot_transfer_prior=prior,
    )
    assert conductor._verify_pilot_baseline is None
    assert conductor.verify_pilot_transfer_reference is None
    # …and the preparer really does route it to that argument, never to a
    # baseline. Source-read for the same reason
    # ``test_the_session_preparer_rearms_the_walked_away_volume_ceiling``
    # uses one: driving ``_open`` needs a live capture.
    source = inspect.getsource(v2host.prepare_v2_session)
    assert "pilot_transfer_prior_from_state(state)" in source
    assert "verify_pilot_transfer_prior=pilot_transfer_prior" in source
    assert "verify_pilot_transfer_baseline" not in source


def test_a_measuring_session_drops_the_prior_level_reference():
    """A pilot transfer is captured THROUGH the applied graph, so once a new
    candidate is measured the previous reference answers a different question.
    A measuring session drops it rather than letting the next stage-2 verify
    report a graph change as a level-reference move — the misattribution
    #1924 and #1927 both exist to stop."""
    v2host.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {
            "pilot_transfer_reference": {
                "values": {"summed": -20.0}, "at": 1_700_000_000.0,
            },
        },
    })
    conductor = _rearm_conductor_for_persist(
        "cap_measure_session", {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state()
    assert state["verify_priors"]["pilot_transfer_reference"] is None


# --- endpoint gates (recovery) ----------------------------------------


def test_prepare_refuses_when_volume_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None
        )
    assert "recover" in str(excinfo.value)


def test_prepare_refuses_an_unknown_tier_before_touching_anything(caplog):
    """Flow-simplification §3: the wizard posts the household's explicit tier.
    An id this build does not have must be refused BEFORE any capture
    registration or volume mutation, not silently measured as something else —
    so the gate runs ahead of every other one in the preparer.

    The evidence that THIS gate fired moved when #1833 stopped the raw flow
    text reaching the household: the refusal now carries the classifier's code
    and the journal carries the constraint. Both still separate it from the
    volume-recovery gate below it, which is uncoded and says "recover".
    """
    import logging

    from jasper.active_speaker.crossover_v2.refusal_copy import (
        REASON_PROGRAM_UNPLAYABLE,
    )

    class _Ready:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_Ready())
    caplog.set_level(logging.WARNING, logger=v2host.__name__)
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {"tier": "turbo"}, status={}, run_async=None, camilla_factory=None
        )
    assert excinfo.value.code == REASON_PROGRAM_UNPLAYABLE
    assert "recover" not in str(excinfo.value)
    assert any(
        "unknown commission tier" in r.getMessage() and "turbo" in r.getMessage()
        for r in caplog.records
    )


def test_prepare_refuses_unrepresentable_confirmed_protection_before_bundle(
    monkeypatch,
):
    class _Ready:
        needs_recovery = False

    def _unrepresentable(*_args):
        raise ValueError("unsupported confirmed filter")

    from jasper.active_speaker import branch_chain

    v2host.set_volume_plan_for_tests(_Ready())
    monkeypatch.setattr(v2host, "reconcile_session_volume_for_new_session", lambda *_: None)
    monkeypatch.setattr(
        v2host, "resolve_conductor_context",
        lambda _status: SimpleNamespace(safety_profile={}, role_targets={}),
    )
    monkeypatch.setattr(branch_chain, "confirmed_protection_sections", _unrepresentable)
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda *_: pytest.fail("bundle opened before protection preflight"),
    )
    with pytest.raises(v2host.CrossoverV2Refused, match="confirmed driver protection"):
        v2host.prepare_v2_session({}, status={}, run_async=None, camilla_factory=None)


def test_the_session_preparer_threads_one_tier_into_the_spec_and_the_map():
    """§1.2's whole point: the emitted plan and the conductor's index→phase map
    come from ONE resolved shape, so a tier can never reach one and not the
    other. Read the preparer's own source rather than trusting the call site to
    stay wired — this is the desync the shape value exists to prevent.
    """
    import inspect

    source = inspect.getsource(v2host.prepare_v2_session)
    # ONE resolution, from ONE requested tier — whether that tier came from the
    # body or (#2639) was inherited from the lapsed session's durable state.
    # The literal moved with the inherit; what it pins did not.
    assert 'requested_tier = (raw.get("tier") if raw else None) or None' in source
    assert source.count("resolve_plan_shape(") == 1
    assert "resolve_plan_shape(requested_tier)" in source
    assert "build_v2_session_spec(" in source and "plan_shape=plan_shape" in source
    # Stronger than the old literal: the preparer must READ the one owner of
    # this fact, so the chooser and the session cannot drift (#2098).
    assert "include_cloud_measure = STAGE1_INCLUDES_CLOUD_MEASURE" in source
    assert STAGE1_INCLUDES_CLOUD_MEASURE is False
    # THREE since #2732's angle-walk take: the base index→phase map, the same
    # map rebuilt when a staged walk is taken, and the emitted spec. Every one
    # of them reads the single ``include_cloud_measure`` local above, which is
    # what this count is actually about — a literal at any of the three would
    # be the drift, not the number of call sites.
    assert source.count("include_cloud_measure=include_cloud_measure") == 3
    assert "confirmed_protection_sections(" in source
    assert "protection_sections_by_role=protection_sections" in source
    assert "measurement_protection_sections_by_role=protection_sections" in source
    assert "tier=plan_shape.tier," in source


def test_the_tier_rides_the_durable_state_and_state_block():
    """§1.2: `/state` can tell WHICH instrument produced a result, and an
    unknown one reads as unknown rather than as "full" — the
    ``echo_band_provenance`` discipline (issue #1763)."""
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "tier": "express",
    })
    assert v2status.crossover_v2_status_block()["tier"] == "express"
    # State written before tiers existed says nothing, and nothing is invented.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
    })
    assert v2status.crossover_v2_status_block()["tier"] is None


# --- two-stage commission D4: the prediction on the wire ------------------


def _closed_cloud_conductor():
    """A real conductor walked to its cloud-measure close, so it carries a
    candidate, a full-resolution ``measure_predicted_sum``, and the spec report
    its accountability seam graded that sum with."""
    from tests.crossover_v2_fixtures import (
        FakeSeams,
        _cloud_conductor,
        _eligible_measure_analysis,
        _walk_measure_cloud_to_close,
    )

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _cloud_conductor(fakes)
    _walk_measure_cloud_to_close(conductor)
    return conductor


def test_the_persisted_prediction_verdict_is_the_veto_s_not_a_re_grade():
    """D4's "one grading instrument", at the persistence seam.

    The durable state carries the prediction TWICE in different resolutions —
    the curve at ``MAX_PERSISTED_SUM_POINTS`` (a drawing) and the verdict from
    the full-resolution tuple (the instrument). This pins that the stored
    verdict is the conductor's own, and that re-grading the stored curve would
    have produced something else, so the distinction is load-bearing rather
    than notional."""
    from jasper.active_speaker.crossover_v2_flow import spec_report_for_predicted_sum

    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    priors = v2host.load_v2_state()["verify_priors"]
    assert priors["predicted_spec"] == conductor.measure_predicted_spec_report
    stored_report = dict(priors["predicted_spec"])
    comparison = stored_report.pop("comparison")
    assert comparison["reason"] == "predicted_in_spec"
    assert stored_report == spec_report_for_predicted_sum(
        conductor.measure_predicted_sum
    ).to_dict()

    # The curve that WAS persisted grades differently — which is exactly why
    # the report is persisted instead of being recomputed from it.
    stored_curve = priors["predicted_sum"]
    re_graded = spec_report_for_predicted_sum((
        np.asarray(stored_curve["freqs_hz"], dtype=float),
        np.asarray(stored_curve["magnitude_db"], dtype=float),
    )).to_dict()
    assert re_graded != priors["predicted_spec"]


def test_the_prediction_verdict_survives_a_verify_rearm_persist():
    """The carry-forward, pinned on the shape that has broken three times.

    A verify-only re-arm builds a FRESH conductor that never runs a fit, so
    every MEASURE-owned prior has to travel to it explicitly or the first
    "Try again" blanks it — the ``cloud`` B1 / way-back-stash W6.12 bug
    shape. The verdict rides the same route as ``gate_window_ms``, and this is
    what proves the route is wired at BOTH ends."""
    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)
    stored = v2host.load_v2_state()["verify_priors"]["predicted_spec"]
    assert stored is not None

    rearmed = CrossoverV2Session(
        session_id="cap_rearm",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=conductor._seams,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        measure_predicted_sum=conductor.measure_predicted_sum,
        measure_predicted_spec_report=stored,
    )
    assert rearmed.measure_predicted_spec_report == stored
    v2host.persist_conductor_state(rearmed, failure_code=None)
    assert v2host.load_v2_state()["verify_priors"]["predicted_spec"] == stored


def test_the_prediction_reaches_the_status_block_with_its_verdict():
    """D4's projection: curve + stored verdict, beside the cloud blocks."""
    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    prediction = v2status.crossover_v2_status_block()["prediction"]
    stored = conductor.measure_predicted_spec_report
    assert prediction["overall_passed"] == stored["overall_passed"]
    assert prediction["reference_db"] == pytest.approx(stored["reference_db"])
    # The per-band vocabulary matches the compact cloud block's, key for key,
    # so the review screen can draw both curves in one tolerance corridor.
    assert [set(b) for b in prediction["spec_bands"]] == [
        {"f_lo_hz", "f_hi_hz", "passed", "max_deviation_db", "tolerance_db"}
    ] * len(stored["bands"])
    assert prediction["curve"]["freqs_hz"]


def test_the_predicted_curve_rides_the_existing_chart_decimation_owner():
    """D4: "the chart feed keeps one decimation owner".

    The predicted curve and the cloud curves are drawn in one frame, so they
    must be strided by the SAME function at the SAME ceiling — a second inline
    copy of the stride is how two curves in one chart end up at silently
    different densities. Pinned by handing both projections the identical raw
    curve and requiring identical output.

    **The ceiling is now a HARD one (gate finding on #1858, SF-1) — re-derived,
    not adjusted to match.** This used to read "the ceiling is a SOFT one":
    ``max(1, n // CAP)`` floor-division stride, so a length not a multiple of
    it overshot by up to one stride (1031 raw points strode by 4 and yielded
    258, not 256). That was tolerable only because every persisted length that
    ever reached this function historically overshot its OWN cap (both
    ``_decimate_sum``'s old raw stride and ``_decimate_curve_for_json``'s
    still land at/above 512-513 for a real capture). #1858's block-average fix
    to ``_decimate_sum`` undershoots its cap instead (a 32769-bin capture
    persists at 504, not 512-513) — landing the predicted curve's persisted
    length just below ``CAP * 2``, where the OLD floor-division stride
    computed ``step = 1`` (no reduction at all: 504 rendered, not ~252),
    silently doubling the prediction's density against the cloud curves in
    the same frame and breaking this function's own soft-ceiling promise.
    Fixed at this owner with ceiling division (``-(-n // CAP)``), which
    guarantees ``len(rendered) <= CAP`` unconditionally — re-derived here on
    the SAME 1031-point fixture: ``ceil(1031 / 256) = 5`` (not floor's 4), so
    1031 strode by 5 yields 207, not 258. Both curve families still ride the
    identical function, so the "one owner" pin is unmoved; only the stride
    arithmetic inside that one owner changed, verified by direct sweep (see
    ``test_realized_chart_lengths_stay_within_cap_for_both_curve_families``)
    over 1..5000 plus 2000 random larger lengths: max observed output was
    exactly 256, never more, for any input."""
    n = v2status.CHART_CURVE_MAX_JSON_POINTS * 4 + 7  # not a multiple of the cap
    freqs = [100.0 + i for i in range(n)]
    mags = [float(i % 5) for i in range(n)]
    raw = {"freqs_hz": freqs, "magnitude_db": mags}

    v2host.save_v2_state({
        "session_id": "cap_x",
        "cloud": {
            PHASE_CLOUD_MEASURE: {"pipeline": {"available": True, "curve": raw}},
        },
        "verify_priors": {"predicted_sum": raw},
    })
    block = v2status.crossover_v2_status_block()
    predicted = block["prediction"]["curve"]
    # THE pin: one owner, so identical input yields byte-identical output.
    assert predicted == block["cloud_chart"][PHASE_CLOUD_MEASURE]["curve"]
    assert len(predicted["freqs_hz"]) == len(predicted["magnitude_db"])
    # Genuinely decimated, to exactly the shared owner's (now ceiling-division)
    # stride -- re-derived: ceil(1031 / 256) = 5, not floor's 4.
    stride = -(-n // v2status.CHART_CURVE_MAX_JSON_POINTS)
    assert stride == 5
    assert len(predicted["freqs_hz"]) == len(range(0, n, stride))
    assert len(predicted["freqs_hz"]) == 207
    # The hard ceiling itself: never CAP + stride (the old soft promise),
    # always CAP outright.
    assert len(predicted["freqs_hz"]) <= v2status.CHART_CURVE_MAX_JSON_POINTS


def test_realized_chart_lengths_stay_within_cap_for_both_curve_families():
    """Gate finding on #1858 (SF-1): the constants-only drift guard
    (``test_cloud_curve_max_json_points_mirrors_the_verify_priors_
    decimation_cap`` in ``tests/test_crossover_v2_cloud_pipeline.py``) pins
    ``MAX_PERSISTED_SUM_POINTS == CLOUD_CURVE_MAX_JSON_POINTS`` (512 == 512),
    never the REALIZED wire lengths downstream of them — so it stayed green
    straight through the regression where the predicted curve rendered at
    ~2x the cloud curves' density in the same chart frame (504 points,
    undecimated, next to 257). This test drives both persist-time decimators
    (``_decimate_sum`` for the prediction, ``_decimate_curve_for_json`` for
    the cloud) through the SAME chart-time re-decimation
    (``_decimate_curve_for_chart``) at real FFT-bin grid sizes, and asserts
    what actually reaches the wire, not the constants that feed it.

    Two sizes, both realistic ``np.fft.rfftfreq`` outputs (the shape
    ``predicted_sum`` and the cloud's combined curve actually carry): the
    65536-point FFT window's 32769-bin grid (matches the size
    ``test_predicted_spec_report_is_graded_on_the_shared_analysis_grid``
    already uses as its own "real capture" fixture) and a second, smaller
    16384-point window's 8193-bin grid — so the bound is pinned as a
    property of the functions, not of one fixture that happens to clear it.
    """
    from jasper.active_speaker.crossover_v2.spatial import _decimate_curve_for_json

    for n_fft in (1 << 16, 1 << 14):
        freqs = np.fft.rfftfreq(n_fft, 1.0 / 48000.0)
        mag_db = np.zeros(freqs.size)

        persisted_pred = v2host._decimate_sum((freqs, mag_db))
        rendered_pred = v2status._decimate_curve_for_chart(
            persisted_pred["freqs_hz"], persisted_pred["magnitude_db"],
        )
        persisted_cloud = _decimate_curve_for_json(freqs, mag_db)
        rendered_cloud = v2status._decimate_curve_for_chart(
            persisted_cloud["freqs_hz"], persisted_cloud["magnitude_db"],
        )

        # The hard ceiling itself, for BOTH curve families -- this is what
        # the constants-equality guard could never see.
        assert len(rendered_pred["freqs_hz"]) <= v2status.CHART_CURVE_MAX_JSON_POINTS
        assert len(rendered_cloud["freqs_hz"]) <= v2status.CHART_CURVE_MAX_JSON_POINTS

        # Same-frame density parity: the bug's own signature was an
        # UNBOUNDED mismatch (504 undecimated vs. 257, ~2x and growing with
        # input size, since floor-division gave the prediction NO reduction
        # at all). A generous 2x margin still catches that class outright
        # while tolerating the two decimators' differing raw-stride vs.
        # block-average characters (measured ~1.4-1.5x on these two grids).
        len_pred = len(rendered_pred["freqs_hz"])
        len_cloud = len(rendered_cloud["freqs_hz"])
        assert max(len_pred, len_cloud) <= 2 * min(len_pred, len_cloud), (
            n_fft, len_pred, len_cloud,
        )


def test_decimate_sum_tracks_smoothed_truth_not_the_aliased_stride():
    """Issue #1858: ``_decimate_sum`` must anti-alias before reducing point
    count, not stride-pick raw bins.

    The synthetic curve is a slow, genuine trend (what a persisted prior
    should track) plus a fast ripple whose ~10 Hz period is far shorter than
    the ~46.9 Hz output grid spacing (``24000 / MAX_PERSISTED_SUM_POINTS``)
    -- "ripple faster than the output grid" -- planted across the full
    sweep including the sub-500 Hz region the issue calls out. 500 Hz sits
    inside the old stride's fewer-than-3-samples-per-1/3-octave-band zone
    (below ~607 Hz; below ~202 Hz the stride spacing exceeds the band's own
    width outright, zero guaranteed samples), so a single stride-picked raw
    bin there was noise, not shape.

    Pinned against the regression it fixes, not just that new code runs: the
    naive floor-division stride this replaces is reproduced locally (it no
    longer exists in production after this fix) and demonstrably fails the
    same tolerance the fixed function meets.
    """
    n = 1 << 16
    fs = 48000.0
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    slow_true_db = 3.0 * np.sin(2.0 * np.pi * freqs / 400.0)
    fast_ripple_db = 2.0 * np.sin(2.0 * np.pi * freqs / 10.0)
    mag_db = slow_true_db + fast_ripple_db
    mag_db[0] = slow_true_db[0]  # avoid the f=0 edge

    decimated = v2host._decimate_sum((freqs, mag_db))
    out_freqs = np.asarray(decimated["freqs_hz"])
    out_mag = np.asarray(decimated["magnitude_db"])
    assert len(out_freqs) <= v2host.MAX_PERSISTED_SUM_POINTS
    assert len(out_freqs) < freqs.size  # genuinely decimated

    below_500 = out_freqs < 500.0
    assert below_500.sum() >= 5  # the region actually gets exercised
    truth_below_500 = 3.0 * np.sin(2.0 * np.pi * out_freqs[below_500] / 400.0)
    new_err = np.abs(out_mag[below_500] - truth_below_500)

    def _old_removed_stride_decimate(freqs, mags, cap):
        """The exact shape ``_decimate_sum`` used before #1858: a raw
        floor-division stride. No longer in production; reproduced here so
        the fix is pinned against the regression it replaces."""
        n = len(freqs)
        step = max(1, n // cap)
        return freqs[::step], mags[::step]

    old_freqs, old_mag = _old_removed_stride_decimate(
        freqs, mag_db, v2host.MAX_PERSISTED_SUM_POINTS,
    )
    old_below_500 = old_freqs < 500.0
    old_truth = 3.0 * np.sin(2.0 * np.pi * old_freqs[old_below_500] / 400.0)
    old_err = np.abs(old_mag[old_below_500] - old_truth)

    # The fix: honest tracking of the slow truth below 500 Hz, well inside
    # the ripple's own 2.0 dB amplitude.
    assert np.median(new_err) < 0.5
    # The regression it fixes: the old stride does not track the truth --
    # a stride-picked raw bin is dominated by whichever ripple phase it
    # happened to land on, comparable to the ripple's own amplitude.
    assert np.median(old_err) > 1.0


def test_an_ungraded_prediction_reaches_the_wire_as_unknown_never_a_pass():
    """``None`` is load-bearing on every field of this block.

    Three absences, three honest shapes: no priors at all ⇒ no block; a curve
    with no stored report (a state written before D4, or a prediction the
    evaluator refused) ⇒ the curve with ``overall_passed`` **None** and no
    bands — never ``False``, which would read as a measured failure, and never
    ``True``, which the compact-cloud rule already forbids fabricating."""
    v2host.save_v2_state({"session_id": "cap_x", "verify_priors": None})
    assert v2status.crossover_v2_status_block()["prediction"] is None

    v2host.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {"predicted_sum": None, "predicted_spec": None},
    })
    assert v2status.crossover_v2_status_block()["prediction"] is None

    v2host.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {
            "predicted_sum": {"freqs_hz": [100.0, 200.0], "magnitude_db": [0.0, 0.0]},
            "predicted_spec": None,
        },
    })
    prediction = v2status.crossover_v2_status_block()["prediction"]
    assert prediction["curve"]["freqs_hz"] == [100.0, 200.0]
    assert prediction["overall_passed"] is None
    assert prediction["spec_bands"] == []
    assert prediction["reference_db"] is None


def test_a_pre_burn_down_refusal_still_reaches_the_wire_with_its_verdict(caplog):
    """The 4th ``prediction`` state: report present, curve absent.

    The verdict is stashed BEFORE the improvement gate runs, while
    ``_measure_predicted_sum`` is assigned only after that gate returns — so
    while item 2 still refused, a ``correction_not_an_improvement`` refusal
    persisted the report with ``predicted_sum`` still ``None``. That refusal is
    gone (the nanny burn-down, doctrine deviation (c)) and no live path
    produces the pairing from THAT cause any more, so the state is built here
    rather than walked into. It is still worth pinning, twice over: a speaker
    that ran a round before the burn-down has exactly these bytes on disk, and
    any later refusal between the stash and ``commit_intervention_proposal``
    reproduces the shape.

    The rendering is what must not regress. ``overall_passed`` is a REAL
    ``False``, not the ``None`` that means unknown, and there is no curve to
    draw beside it — the state a review screen is most likely to get wrong.

    **The retired code is tolerated, not honoured.** ``post_apply_grade`` used
    to read this exact literal into a ``keep_previous`` outcome; that clause
    went with the refusal, so the same bytes now yield no outcome claim at all
    — which is the honest reading of a not-applied round with no selector
    evidence, and specifically not a crash or a fabricated verdict.
    """
    from tests.crossover_v2_fixtures import (
        FakeSeams,
        _cloud_conductor,
        _eligible_measure_analysis,
    )

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _cloud_conductor(fakes)
    # The pre-burn-down pairing, stated directly: item 2 graded the prediction
    # and stashed the report, then refused before any curve was committed.
    conductor._measure_predicted_spec_report = {
        "overall_passed": False,
        "reference_db": 0.0,
        "bands": [{
            "f_lo_hz": 200.0, "f_hi_hz": 2000.0, "tolerance_db": 3.0,
            "max_deviation_db": 4.0, "max_deviation_hz": 1000.0,
            "rms_deviation_db": 2.0, "n_bins": 100, "n_excluded": 0,
            "evaluable": True, "passed": False,
        }],
        "comparison": {
            "reason": "correction_not_an_improvement",
            "baseline_rms_db": 2.0, "selected_rms_db": 2.0,
            "improvement_db": 0.0, "required_db": 0.5,
        },
    }
    assert conductor.candidate is None
    assert conductor.measure_predicted_sum is None
    with caplog.at_level(logging.INFO, logger=v2host.__name__):
        v2host.persist_conductor_state(
            conductor, failure_code="correction_not_an_improvement",
        )
        v2host.persist_conductor_state(
            conductor, failure_code="correction_not_an_improvement",
        )
        v2status.crossover_v2_status_block()
    priors = v2host.load_v2_state()["verify_priors"]
    assert priors["predicted_sum"] is None
    assert priors["predicted_spec"] is not None

    prediction = v2status.crossover_v2_status_block()["prediction"]
    assert prediction["curve"] is None
    # A graded miss, NOT an ungradeable unknown.
    assert prediction["overall_passed"] is False
    assert prediction["spec_bands"]
    assert prediction["reference_db"] is not None
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == "not_applied"
    assert grade["graded"] is True
    # No outcome, and therefore no classification line: the round is graded as
    # not-applied, and there is nothing left that claims to know what it meant.
    assert "outcome" not in grade
    assert "event=correction.crossover_v2_result_classified" not in caplog.text


def test_a_candidate_persisted_now_records_which_headroom_era_stamped_it():
    """D3/D4's era stamp, at the only place that can honestly write it.

    A candidate this function serializes was built by THIS process, so its
    per-fit charges are the CURRENT rule by construction. The stamp is recorded
    here rather than inferred downstream because nothing on a persisted fit
    distinguishes the derivations.

    The value moved with #2758: the realized peak is now evaluated over the
    whole domain, and that era can read SMALLER than a ``realized_peak`` stamp
    for the same filters — the one direction the earlier eras never had — so it
    needs its own name rather than riding the old one."""
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK,
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )

    conductor = _closed_cloud_conductor()
    v2host.persist_conductor_state(conductor, failure_code=None)

    candidate = v2host.load_v2_state()["candidate"]
    assert candidate["headroom_cost_basis"] == (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN
    )
    assert candidate["headroom_cost_basis"] != HEADROOM_COST_BASIS_REALIZED_PEAK
    assert isinstance(candidate["headroom_cost_db"], float)


def test_apply_endpoint_requires_current_candidate():
    with pytest.raises(v2host.CrossoverV2Refused):
        _apply(
            {"expected_candidate_fingerprint": "fp"}, None, None
        )
    # A stale fingerprint against a persisted candidate is refused by name.
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-current"},
    })
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _apply(
            {"expected_candidate_fingerprint": "fp-stale"}, None, None
        )
    assert "no longer current" in str(excinfo.value)


def test_observe_apply_success_arms_the_deferred_verify_gate():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    assert v2host._applied_gate() is False
    # A mismatched fingerprint must NOT arm verify.
    v2host.observe_apply_success("fp-other")
    assert v2host._applied_gate() is False
    v2host.observe_apply_success("fp-1")
    assert v2host._applied_gate() is True


def test_observe_apply_success_clears_a_stale_apply_blocked_nudge():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
        "apply_blocked": {"id": "baseline_profile_not_ready_to_apply", "message": "x"},
    })
    v2host.observe_apply_success("fp-1")
    assert v2host.load_v2_state()["apply_blocked"] is None


def test_save_v2_state_refuses_a_non_finite_number_and_writes_nothing():
    """#2839: the writer fails, not the packet.

    The crossover-v2 evidence packet copies fields out of this state verbatim
    and fingerprints them, and ``evidence_identity.json_fingerprint`` refuses a
    non-finite number — so a NaN banked here costs the round its WHOLE evidence
    packet, at a reader, hours after the code that produced it returned.
    ``allow_nan=False`` moves the failure to this writer, where that code is
    still on the stack.

    Nothing half-written, and that is structural rather than lucky:
    ``json.dumps`` raises while evaluating an ARGUMENT, so ``atomic_write_text``
    is never entered and the prior state is still on disk afterwards.
    """
    v2host.save_v2_state({"session_id": "cap_ok", "applied": False})
    good = v2host.load_v2_state()

    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            v2host.save_v2_state({
                "session_id": "cap_bad",
                "verify": {"claims": {"residual_db": bad}},
            })
        assert v2host.load_v2_state() == good


def test_observe_apply_success_records_the_way_back_pointer():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    v2host.observe_apply_success("fp-1", previous_candidate_fingerprint="fp-prev")
    assert v2host.load_v2_state()["previous_candidate_fingerprint"] == "fp-prev"
    # The speaker's first-ever apply has nothing to point back to, and a
    # later apply that displaced a non-measured profile clears the pointer.
    v2host.observe_apply_success("fp-1", previous_candidate_fingerprint=None)
    assert v2host.load_v2_state()["previous_candidate_fingerprint"] is None


def test_attempt_loop_status_is_minimal_and_start_over_keeps_its_basis():
    loop = {
        "history": [
            {
                "attempt_id": "candidate-a",
                "metric": "max_db_notch_excluded",
                "provenance": "realized",
                "integrity": {"comparable": True, "reasons": []},
                "repeats_used": 1,
                "grade_db": 0.9,
            }
        ],
        "last_decision": {
            "decision": "continue",
            "reason": "baseline_established",
            "basis_attempt_ids": ["candidate-a"],
            "provenance": "realized",
            "floor": {"claim_floor_db": 0.17},
        },
    }
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "attempts_loop": loop,
    })

    from jasper.active_speaker.model_error_store import record_model_error

    for index in range(7):
        record_model_error(
            speaker_id="speaker-a",
            attempt_id=f"candidate-{index}",
            metric="max_db_notch_excluded",
            predicted_db=0.0,
            realized_db=float(index),
        )

    block = v2status.crossover_v2_status_block()
    assert block["attempts_loop"] == {
        "last_decision": loop["last_decision"],
        "store_count": 7,
    }
    assert "history" not in block["attempts_loop"]

    v2host.reset_v2_journey_state()
    assert v2host.load_v2_state()["attempts_loop"] == loop


def test_status_block_surfaces_apply_blocked():
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
        "apply_blocked": {"id": "measured_candidate_preset_mismatch", "message": "x"},
    })
    assert v2status.crossover_v2_status_block()["apply_blocked"] == {
        "id": "measured_candidate_preset_mismatch", "message": "x",
    }


def test_status_block_reports_an_applied_but_ungraded_result():
    """PR-L4 item 4: applied implies graded, and when it does not, `/state`
    says so in its own field rather than leaving an empty `verify` block for
    every surface to read as "nothing to report" — which is how a 10 dB-dark
    profile sat on JTS3 under a green tick."""
    v2host.save_v2_state({
        "session_id": "cap_ungraded",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
    })
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_UNVERIFIED
    assert grade["graded"] is False
    assert grade["verify_outcome"] is None


def test_status_block_reports_a_graded_result_from_either_instrument():
    """Both a passing VERIFY and a graded post-apply cloud are real checks, and
    the tiers differ in which one they run — express omits the post-apply group
    entirely, so keying only on the cloud would call every express session
    ungraded."""
    v2host.save_v2_state({
        "session_id": "cap_graded_verify",
        "applied": True,
        "verify": {"outcome": "pass"},
    })
    by_verify = v2status.crossover_v2_status_block()["post_apply_grade"]
    # Verified at the mark only — express's whole grade, and distinguishable
    # from a walked post-apply group WITHOUT consulting `tier`.
    assert by_verify["state"] == v2host.GRADE_MARK_VERIFIED
    assert by_verify["graded"] is True

    # The cloud instrument grading ALONE. #2464 moved this fixture off
    # ``outcome="inconclusive"`` — that pairing now grades ``inconclusive``,
    # and pinning it here pinned the mask instead of the claim this test
    # makes. A session whose VERIFY produced no outcome at all is the honest
    # way to ask "does a closed group grade on its own".
    v2host.save_v2_state({
        "session_id": "cap_graded_cloud",
        "applied": True,
        "cloud": {
            PHASE_CLOUD_VERIFY: {
                "geometry": {"locked": False},
                "pipeline": {
                    "available": True,
                    "spec": {"overall_passed": False, "bands": []},
                    "merged_excluded_bands_hz": [],
                },
                "session_id": "cap_graded_cloud",
            },
        },
    })
    by_cloud = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert by_cloud["state"] == v2host.GRADE_GRADED
    # A grade that exists and FAILED is still a grade — "we checked and it is
    # out of spec" is a different claim from "we never checked", and item 7's
    # headline is what renders the first one.
    assert by_cloud["post_apply_spec_passed"] is False


# --- R19 honest grading: scope, the spatial gauge's own state (#2098/#2160) --


def _applied_state(*, tier=None, verify_outcome="pass", cloud_verify=None,
                   claims=None):
    """An applied session, optionally with a post-apply cloud group."""
    state = {
        "session_id": "cap_r19",
        "session_phases": [PHASE_VERIFY, PHASE_CLOUD_VERIFY],
        "applied": True,
        "verify": {
            "outcome": verify_outcome,
            **({"claims": claims} if claims is not None else {}),
        },
    }
    if tier is not None:
        state["tier"] = tier
    if cloud_verify is not None:
        state["cloud"] = {PHASE_CLOUD_VERIFY: cloud_verify}
    return state


def _honest_result_state(
    *, tracking="pass", absolute="fail", improvement=0.8, applied=True,
    verify_outcome=None, absolute_evidence=True,
):
    absolute_claim = (
        {
            "status": absolute, "max_db": 4.3139 if absolute == "fail" else 0.8,
            "worst_db": -4.3139 if absolute == "fail" else -0.8,
            "worst_hz": 1590.4083, "tolerance_db": 2.0,
        }
        if absolute != "not_evaluated"
        else {"status": "not_evaluated", "reason": "no_trusted_region"}
    )
    if not absolute_evidence:
        absolute_claim = {"status": absolute}
    stage1 = [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE]
    return {
        "session_id": "cap_p04", "tier": "express", "applied": applied,
        "session_phases": stage1, "accepted_phases": stage1,
        "candidate": {"fingerprint": "fp-p04"},
        "verify": {
            "outcome": verify_outcome or ("fail" if tracking == "fail" else "pass"),
            "claims": {
                "integration": {
                    "status": tracking, "max_db": 1.398262557,
                    "tolerance_db": 1.5,
                },
                "absolute": absolute_claim,
            },
        },
        "verify_priors": {"predicted_spec": {
            "overall_passed": False, "bands": [],
            "comparison": {
                "reason": (
                    "improved" if improvement >= 0.5
                    else "not_an_improvement"
                ),
                "baseline_rms_db": 2.0, "selected_rms_db": 2.0 - improvement,
                "improvement_db": improvement, "required_db": 0.5,
            },
        }},
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"absolute": "pass"}, "verified_target"),
        ({}, "verified_best_evaluated"),
        ({"tracking": "fail"}, "keep_previous"),
        ({"improvement": 0.1}, "keep_previous"),
        ({"absolute": "not_evaluated"}, "inconclusive"),
        ({"verify_outcome": "future"}, "inconclusive"),
        ({"absolute_evidence": False}, "inconclusive"),
        # Not applied: NO route to a verdict is left. A not-an-improvement
        # refusal stopped refusing when accountability's item 2 became a grade
        # (#2854), and the corner selector's ``recommend_alternative`` — the
        # only other cause — is retired here (ticket 2.4). So an un-applied
        # round publishes no outcome at all rather than inventing one.
        ({"applied": False}, None),
    ),
)
def test_honest_result_truth_table(changes, expected):
    v2host.save_v2_state(_honest_result_state(**changes))
    block = v2status.crossover_v2_status_block()
    grade = block["post_apply_grade"]
    assert grade.get("outcome") == expected
    if changes.get("applied", True):
        assert grade["candidate_fingerprint"] == "fp-p04"
    else:
        assert block["phase"] == "review"
    if expected == "verified_best_evaluated":
        assert grade["tracking_passed"] is True
        assert grade["absolute_passed"] is False
        assert grade["absolute_miss_db"] == 4.3139
        assert grade["absolute_worst_hz"] == 1590.4083


def _no_sweep_state(*, fc_selection=None):
    """A finished commission whose stage 1 ran no candidate sweep.

    The stage-1 phases are DERIVED from the stage-1 flags, so this IS the
    shipped shape rather than a hand-written guess at it. What the tests below
    turn on is the absent ``fc_selection``: no stage-1 plan builds a lateral
    group any more, and both the sweep that used to fire off one and the
    selector that scored it are retired, so no session — shipped or otherwise —
    banks a fresh selection.

    ``fc_selection`` is still a parameter because a speaker whose last round ran
    under a build that HAD a selector still carries one in durable state, and
    the tests below use it to pin that such a payload is inert rather than
    refused.

    Deliberately NOT asserting which phases came back. The tests below are about
    behaviour that must hold whether or not ``fc_selection`` is present at all,
    so pinning the shape in the shared fixture would make them fail for a reason
    they are not about. The shipped shape has its own pin in
    ``test_crossover_v2_lateral_evidence.py``.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_VERIFY
    from jasper.active_speaker.crossover_v2_flow import (
        STAGE1_INCLUDES_CLOUD_MEASURE,
        STAGE1_INCLUDES_ENTRY_BASELINE,
    )

    stage1 = list(dict.fromkeys(build_v2_cloud_index_phase_map(
        tier="express",
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
    ).values()))
    # …then stage 2's own session, which is what carries the household past the
    # apply to the done screen. The whole journey, as a finished commission.
    phases = [*stage1, PHASE_VERIFY]
    state = {
        "session_id": "cap_pause", "tier": "express", "applied": True,
        "session_phases": phases, "accepted_phases": phases,
        "candidate": {"fingerprint": "fp-pause"},
        "verify": {
            "outcome": "pass",
            "claims": {
                "integration": {
                    "status": "pass", "max_db": 1.398262557, "tolerance_db": 1.5,
                },
                "absolute": {
                    "status": "pass", "max_db": 0.8, "worst_db": -0.8,
                    "worst_hz": 1590.4083, "tolerance_db": 2.0,
                },
            },
        },
        "verify_priors": {"predicted_spec": {
            "overall_passed": False, "bands": [],
            "comparison": {
                "reason": "improved", "baseline_rms_db": 2.0,
                "selected_rms_db": 1.2, "improvement_db": 0.8, "required_db": 0.5,
            },
        }},
    }
    if fc_selection is not None:
        state["fc_selection"] = fc_selection
    return state


def test_a_paused_walk_commission_still_grades_verified():
    """The coupling the 2026-08-18 lateral pause exposed, pinned end to end.

    ``_post_apply_grade`` gated its success verdicts on ``comparison_complete``
    and ``authorized_winner``, both of which read an ``fc_selection`` the
    shipped session no longer banks. Absence read as an unfinished comparison,
    so ``verified_target`` became structurally unreachable and every successful
    commission told the household "not enough complete evidence to grade… this
    report changed nothing automatically" — false over an applied tune.

    The post-apply grade answers "was the applied correction checked
    afterwards". VERIFY answered it here; no selector was consulted, and none
    had to be.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_ENTRY_BASELINE
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    # This test is specifically about the SHIPPED shape, so it says so here
    # rather than in the shared fixture: stage 1 walks no poses, which is
    # exactly why no sweep runs and no selection is banked.
    state = _no_sweep_state()
    assert state["session_phases"][:3] == [
        PHASE_CHECK, PHASE_MEASURE, PHASE_ENTRY_BASELINE,
    ]

    v2host.save_v2_state(state)
    block = v2status.crossover_v2_status_block()
    grade = block["post_apply_grade"]

    assert grade["outcome"] == "verified_target"
    assert grade["graded"] is True
    assert grade["complete"] is True
    assert grade["candidate_fingerprint"] == "fp-pause"
    # The absent sweep is reported as absent rather than as a failed comparison:
    # no selector fact is published at all, in either direction.
    assert "comparison_complete" not in grade
    assert block.get("fc_selection") is None

    text = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": block,
    })["verdict_text"]
    assert "reached the target" in text
    assert "changed nothing automatically" not in text


def test_a_legacy_fc_selection_is_inert_and_never_refuses():
    """Read-back tolerance for the retired field, by NON-CONSUMPTION (2.4).

    A speaker whose last round ran under a build that still had a corner
    selector carries that round's ``fc_selection`` in durable state forever.
    No PRODUCT read path parses it — the grade, the status block and the
    household envelope all reach their answers without touching the field — so
    no legacy shape, well-formed or not, can refuse or raise. (The offline
    archaeology scripts still read it on purpose;
    ``scripts/derive-crossover-incident-fixture.py`` mints the #2291 fixture
    from exactly this payload, which is why it is preserved rather than
    scrubbed — asserted at the end of this test.) That is what
    "versioned-absent field, readers degrade gracefully" buys: the tolerance is
    structural rather than a per-shape guard someone has to maintain.

    Degrading means the round grades on its OWN verification evidence, which is
    measured fact about the applied tune. It does NOT mean restating a retired
    comparator's opinion of an alternative — this build cannot check that
    opinion, and a badge it cannot check is a badge it must not print.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    legacy_shapes = (
        # The three verdicts a real selector could reach…
        {"verdict": "keep_configured", "configured_hz": 2000.0,
         "recommended_hz": None, "comparison_complete": True,
         "scores": [{"fc_hz": 2000.0, "score": 3.0},
                    {"fc_hz": 1800.0, "score": 3.2}]},
        {"verdict": "recommend_alternative", "configured_hz": 2000.0,
         "recommended_hz": 1800.0, "margin_db": 1.4, "evaluated": 2,
         "planned": 2, "attempted": [2000.0, 1800.0], "limits": {},
         "comparison_complete": True, "scores": [{"fc_hz": 1800.0, "score": 1.6}]},
        {"verdict": "no_alternative_evaluated", "configured_hz": 2000.0,
         "recommended_hz": None, "comparison_complete": False, "scores": []},
        # …the one that used to gate this badge to `inconclusive`, which is the
        # behaviour change this test is the record of…
        {"verdict": "keep_configured", "configured_hz": 2000.0,
         "recommended_hz": None, "comparison_complete": False,
         "scores": [{"fc_hz": 2000.0, "score": 3.0}]},
        # …and three shapes no reader may assume anything about: half-written,
        # wrong types throughout, and not a mapping at all.
        {"verdict": "keep_configured"},
        {"verdict": 17, "configured_hz": "two thousand", "scores": {"nope": True},
         "comparison_complete": "yes", "limits": None},
        "recommend_alternative",
    )
    for legacy in legacy_shapes:
        v2host.save_v2_state(_no_sweep_state(fc_selection=legacy))
        block = v2status.crossover_v2_status_block()

        # Graded from VERIFY alone, identically to the same round without it.
        assert block["post_apply_grade"]["outcome"] == "verified_target", legacy
        assert "comparison_complete" not in block["post_apply_grade"]
        # Never re-published, so no surface can render a corner the retired
        # selector once named.
        assert "fc_selection" not in block, legacy

        text = build_crossover_envelope_v2({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": block,
        })["verdict_text"]
        assert "1800" not in text and "measured better than" not in text

        # The durable payload is untouched by the read — inert, not scrubbed.
        assert (v2host.load_v2_state() or {})["fc_selection"] == legacy


def test_terminal_result_logs_once_with_target_failure_evidence(caplog):
    prior = _honest_result_state()
    v2host.save_v2_state(prior)

    class TerminalConductor(_StubConductor):
        verify_outcome = "pass"
        verify_claims = prior["verify"]["claims"]
        measure_predicted_spec_report = prior["verify_priors"]["predicted_spec"]

        def snapshot(self):
            return SimpleNamespace(
                session_id="cap_p04", accepted_phases=(PHASE_VERIFY,),
                session_phases=(PHASE_VERIFY,), tier="express", applied=True,
                gain_plan_db=None, candidate_fingerprint=None, cloud_close="",
            )

    conductor = TerminalConductor("cap_p04")
    with caplog.at_level(logging.INFO, logger=v2host.__name__):
        v2host.persist_conductor_state(conductor, failure_code=None)
        v2host.persist_conductor_state(conductor, failure_code=None)
        v2status.crossover_v2_status_block()
    lines = [
        record.message for record in caplog.records
        if "event=correction.crossover_v2_result_classified" in record.message
    ]
    assert len(lines) == 1
    assert "outcome=verified_best_evaluated" in lines[0]
    assert "absolute_passed=false" in lines[0]
    assert "absolute_miss_db=4.3139" in lines[0]
    assert "absolute_worst_hz=1590.4083" in lines[0]
    assert "candidate_fingerprint=fp-p04" in lines[0]


def test_terminal_result_log_tolerates_a_malformed_projection(monkeypatch, caplog):
    conductor = _StubConductor("cap_malformed")
    conductor.snapshot = lambda: SimpleNamespace(
        session_id="cap_malformed", accepted_phases=(PHASE_VERIFY,),
        session_phases=(PHASE_VERIFY,), tier="", applied=True,
        gain_plan_db=None, candidate_fingerprint=None, cloud_close="",
    )
    monkeypatch.setattr(
        v2status, "crossover_v2_status_block", lambda: {"post_apply_grade": None},
    )
    with caplog.at_level(logging.INFO, logger=v2host.__name__):
        v2host.persist_conductor_state(conductor, failure_code=None)
    assert "outcome=inconclusive" in caplog.text


_NO_GAUGE = object()  # "this era wrote no flatness key", vs. an explicit None


def _closed_cloud_group(*, passed, flatness=_NO_GAUGE):
    """A closed post-apply group in DURABLE shape, as the conductor writes it."""
    pipeline = {
        "available": True,
        "spec": {"overall_passed": passed, "bands": []},
        # Four excluded intervals — the jts3 2026-08-07 shape.
        "merged_excluded_bands_hz": [
            [1400.0, 1900.0], [3000.0, 3200.0], [5000.0, 5400.0], [9000.0, 9600.0],
        ],
    }
    if flatness is not _NO_GAUGE:
        pipeline["flatness"] = flatness
    return {
        # Never locked — the same checkpoint fact the cloud-pipeline doctor
        # line prints beside this group.
        "geometry": {"locked": False},
        "pipeline": pipeline,
        "session_id": "cap_r19",
    }


_GRADED_AND_FAILED_FLATNESS = {
    "max_db": -4.628, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
    "tolerance_db": 1.5, "rms_db": 1.9, "n_bins": 700, "n_excluded": 40,
    "evaluable": True, "passed": False,
}


def test_a_closed_post_apply_group_that_failed_grades_as_failed_not_as_green():
    """#2160 — the jts3 2026-08-07 shape, reproduced.

    ``overall_passed=False`` reaches ``GRADE_GRADED`` because a
    graded-and-failed group IS graded, and every consuming surface read that
    state name as a clean result: doctor printed ``applied and graded
    (state=graded, verify=pass)`` beside a cloud line reading ``spec=fail
    worst=-4.63dB``. ``state`` cannot carry the difference; ``spatial`` does,
    and the failing gauge's own number rides with it so no consumer re-derives
    it. The ruling is grade-and-disclose: the tune stays, the failure is
    loud."""
    v2host.save_v2_state(_applied_state(
        tier=TIER_FULL,
        cloud_verify=_closed_cloud_group(
            passed=False, flatness=_GRADED_AND_FAILED_FLATNESS,
        ),
    ))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_GRADED  # unchanged vocabulary
    assert grade["spatial"] == v2host.GRADE_SPATIAL_FAILED
    # A failed grade is a COMPLETED grade — the tier delivered what it
    # promised, and what it delivered is a miss.
    assert grade["scope"] == v2host.GRADE_SCOPE_SPATIAL
    assert grade["complete"] is True
    assert grade["spatial_worst_db"] == pytest.approx(-4.628)
    assert grade["spatial_worst_hz"] == pytest.approx(1650.0)


def test_a_full_session_that_only_verified_at_the_mark_is_incomplete():
    """#2098's own field evidence: Full, ``verify.outcome=pass``, ``cloud``
    absent — a true local result rendered as the wider claim. The local pass
    is preserved; what is added is that it is not what Full promised."""
    v2host.save_v2_state(_applied_state(tier=TIER_FULL))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_MARK_VERIFIED  # the local pass stands
    assert grade["graded"] is True
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["spatial"] == v2host.GRADE_SPATIAL_ABSENT
    assert grade["complete"] is False


def test_an_express_session_verified_at_the_mark_is_complete_and_scoped():
    """Express structurally never walks a post-apply group, so the mark IS its
    whole promise. Judging it against Full's would warn every express session
    ever run — the mirror of the defect."""
    v2host.save_v2_state(_applied_state(tier=TIER_EXPRESS))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["scope"] == v2host.GRADE_SCOPE_MARK
    assert grade["complete"] is True


_PASSING_GROUP = {"passed": True, "flatness": {
    **_GRADED_AND_FAILED_FLATNESS, "max_db": 0.9, "passed": True,
}}

# ``SpecFlatness.passed`` is False for a spectrum no band survived to measure,
# by its own "will not report a clean bill of health for a spectrum it could
# not fully measure" rule — a miss and an unmeasurable are not the same fact.
_UNMEASURABLE_FLATNESS = {
    **_GRADED_AND_FAILED_FLATNESS,
    "max_db": None, "max_hz": None, "evaluable": False, "passed": False,
}

_PASSING = _closed_cloud_group(**_PASSING_GROUP)


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        pytest.param(
            {"tier": TIER_FULL, "cloud_verify": _PASSING},
            # No number beside a pass: printing the margin of a pass next to a
            # failure verdict is how the two get confused.
            {"spatial": v2host.GRADE_SPATIAL_PASSED,
             "scope": v2host.GRADE_SCOPE_SPATIAL, "complete": True,
             "spatial_worst_db": None, "spatial_worst_hz": None},
            id="closed-and-passing-group-is-a-complete-spatial-grade",
        ),
        # Reading an unmeasurable spectrum as a miss states a measurement that
        # never happened. No spatial CLAIM exists, so the delivered width falls
        # back to what the mark proved — on Full, short of the promise.
        pytest.param(
            {"tier": TIER_FULL, "cloud_verify": _closed_cloud_group(
                passed=False, flatness=_UNMEASURABLE_FLATNESS)},
            {"spatial": v2host.GRADE_SPATIAL_UNMEASURABLE,
             "spatial_worst_db": None, "scope": v2host.GRADE_SCOPE_MARK,
             "complete": False},
            id="an-ungradeable-group-is-not-a-failure",
        ),
        # Unmeasurable is claimed only on POSITIVE evidence: a state written
        # before the gauge shipped carries a real ``overall_passed=False`` and
        # no ``flatness``, and downgrading that on the ABSENCE of an instrument
        # is the fabricated reading pointed the other way.
        pytest.param(
            {"tier": TIER_FULL, "cloud_verify": _closed_cloud_group(passed=False)},
            {"spatial": v2host.GRADE_SPATIAL_FAILED, "spatial_worst_db": None},
            id="a-failing-group-with-no-gauge-stays-a-failure",
        ),
        # A pre-tier state file, or one from a later build: this build cannot
        # know what was promised, and manufacturing an incompleteness warning
        # about a promise it never read is worse than saying what it said.
        pytest.param(
            {"tier": None},
            {"scope": v2host.GRADE_SCOPE_MARK, "complete": True},
            id="an-unreadable-tier-is-judged-on-delivery",
        ),
        pytest.param(
            {"tier": "tier-from-2027"}, {"complete": True},
            id="a-tier-from-the-future-is-judged-on-delivery",
        ),
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "inconclusive"},
            {"state": v2host.GRADE_INCONCLUSIVE,
             "scope": v2host.GRADE_SCOPE_NONE, "complete": False},
            id="a-verify-that-did-not-pass-delivers-no-scope",
        ),
        # #2464: a failed or undecided mark-VERIFY caps the badge whatever the
        # group says. ``cloud_verdict`` was tested BEFORE the fail arm, so any
        # closed group masked it. The spatial instrument's own verdict is
        # untouched and still rides its own field (#2160 rider) — capping the
        # badge is not co-locating the two facts.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "fail",
             "claims": {"integration": {"status": "fail", "max_db": 4.2}},
             "cloud_verify": _PASSING},
            {"state": v2host.GRADE_FAILED, "graded": False,
             "spatial": v2host.GRADE_SPATIAL_PASSED,
             "post_apply_spec_passed": True},
            id="a-failed-verify-is-not-masked-by-a-passing-spatial-grade",
        ),
        # ``verify.outcome`` grades capture and tracking health ONLY, so a
        # crossover-region claim that missed its tolerance rides a clean
        # ``pass``: the CLAIMS record is the source, and both facts stand.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "pass",
             "claims": {"integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "fail", "max_db": 4.31,
                                     "worst_hz": 1590.4}},
             "cloud_verify": _PASSING},
            {"state": v2host.GRADE_FAILED, "graded": False,
             "verify_outcome": "pass"},
            id="a-failed-absolute-claim-caps-the-badge-on-a-clean-capture",
        ),
        # The two instruments are a UNION, not a fallback: an ``outcome`` fail
        # whose claims are ``not_evaluated`` still caps.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "fail",
             "claims": {"integration": {"status": "not_evaluated"},
                        "absolute": {"status": "not_evaluated",
                                     "reason": "no_trusted_region"}},
             "cloud_verify": _PASSING},
            {"state": v2host.GRADE_FAILED},
            id="an-outcome-fail-whose-claims-could-not-grade-still-caps",
        ),
        # The same masking defect one arm over: the ``inconclusive`` arm was
        # unreachable behind the closed-group test.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "inconclusive",
             "cloud_verify": _closed_cloud_group(passed=False)},
            {"state": v2host.GRADE_INCONCLUSIVE, "graded": False},
            id="an-inconclusive-verify-is-not-masked-by-a-closed-group",
        ),
        # The cap is scoped to a FAILED or undecided VERIFY. On a clean pass
        # the walked group is the wider claim and still wins the state word,
        # or this demotes every correctly graded Full session.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "pass",
             "claims": {"integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "pass", "max_db": 0.8}},
             "cloud_verify": _PASSING},
            {"state": v2host.GRADE_GRADED, "graded": True, "complete": True},
            id="a-clean-pass-still-grades-on-the-wider-spatial-claim",
        ),
        # Absence of claims is a pre-R18 state file, never a fail and never a
        # pass-of-claims: the outcome stands as the only record there is.
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "pass", "cloud_verify": _PASSING},
            {"state": v2host.GRADE_GRADED},
            id="no-claims-block-graded-on-a-passing-outcome-alone",
        ),
        pytest.param(
            {"tier": TIER_FULL, "verify_outcome": "fail", "cloud_verify": _PASSING},
            {"state": v2host.GRADE_FAILED},
            id="no-claims-block-graded-on-a-failing-outcome-alone",
        ),
    ),
)
def test_the_post_apply_grade_badge_table(state, expected):
    """What ``post_apply_grade`` publishes for each shape of applied session.

    ``state`` is the vocabulary every consuming surface keys on; ``spatial``,
    ``scope`` and ``complete`` say how wide the claim is and whether the tier
    delivered what it promised. A row asserts only the fields its own shape
    decides — the rest are pinned by the rows that turn on them.
    """
    v2host.save_v2_state(_applied_state(**state))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    for key, value in expected.items():
        assert grade[key] == value, key


def test_status_block_never_asks_an_unapplied_session_for_a_grade():
    v2host.save_v2_state({"session_id": "cap_none", "applied": False})
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2host.GRADE_NOT_APPLIED
    # Nothing promised, so nothing outstanding: `complete=False` here would
    # warn every speaker that has never been commissioned.
    assert grade["complete"] is True
    assert grade["scope"] == v2host.GRADE_SCOPE_NONE
    assert grade["spatial"] == v2host.GRADE_SPATIAL_ABSENT
    assert grade["graded"] is True


def test_end_to_end_the_done_screen_offers_the_way_back_only_with_a_prior_candidate(
    monkeypatch,
):
    """Contract test over the REAL production seam — save_v2_state ->
    crossover_v2_status_block -> build_crossover_envelope_v2 — exactly what a
    GET /crossover/envelope on the done screen serves, not a hand-built
    envelope fixture. A first-ever apply (no recorded prior candidate) offers no way
    back; a stash naming a measured candidate mints the republish action."""
    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
    from jasper.web import correction_crossover_v2_republish as republish_door

    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    # This contract is the status->envelope seam; the bank behind the door's
    # read-only admission has its own suites. The admitted shape is stubbed;
    # the refused shape has a dedicated pin below.
    monkeypatch.setattr(republish_door, "republish_preflight", lambda fp: None)

    def _envelope_for(previous_candidate_fingerprint):
        v2host.save_v2_state({
            "session_id": "cap_e2e",
            "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
            "applied": True,
            "previous_candidate_fingerprint": previous_candidate_fingerprint,
            "verify": {"outcome": "pass"},
        })
        status = {
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": v2status.crossover_v2_status_block(),
        }
        return build_crossover_envelope_v2(status)

    first_ever = _envelope_for(None)
    assert first_ever["screen"] == "done"
    assert first_ever["next_action"]["id"] == "room"
    assert not any(
        a["id"] == "republish_previous" for a in first_ever["alternate_actions"]
    )

    with_prior = _envelope_for("f" * 64)
    assert with_prior["screen"] == "done"
    assert with_prior["next_action"]["id"] == "room"
    way_back = next(
        a for a in with_prior["alternate_actions"]
        if a["id"] == "republish_previous"
    )
    assert way_back["endpoint"] == "/correction/crossover/v2/republish"
    assert way_back["body"] == {"fingerprint": "f" * 64}


def test_blocking_apply_issue_prefers_a_blocker_over_earlier_non_blocker_issues():
    payload = {
        "issues": [
            {"severity": "info", "code": "manual_crossover_preserved", "message": "kept"},
            {"severity": "blocker", "code": "the_real_reason", "message": "why"},
            {"severity": "blocker", "code": "generic_trailer", "message": "trailer"},
        ]
    }
    assert v2host._blocking_apply_issue(payload) == {
        "id": "the_real_reason", "message": "why",
    }


def test_blocking_apply_issue_none_when_no_issues():
    assert v2host._blocking_apply_issue({"issues": []}) is None
    assert v2host._blocking_apply_issue({}) is None


# --- production analyze binding (geometry + calibration) --------------------------


def _mono_wav_bytes(n: int = 4800) -> bytes:
    import io

    from scipy.io import wavfile

    buf = io.BytesIO()
    wavfile.write(buf, 48000, np.zeros(n, dtype=np.int16))
    return buf.getvalue()


class _FakeResult:
    def __init__(self, setup=None, device=None, capture_integrity=None) -> None:
        self.wav = _mono_wav_bytes()
        self.setup = setup
        self.device = device
        # The phone's own per-take report (#2151), which #2094 reconciles
        # against the frames this host decodes. `None` is what every capture
        # from an older page bundle carries.
        self.capture_integrity = capture_integrity


def test_production_analyze_threads_geometry_and_resolved_calibration(monkeypatch):
    """bind_production_analyze forwards the conductor's geometry AND the
    resolved calibration curve into analyze_program_capture."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen.update(calibration=calibration, geometry=geometry, rate=rate)
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    curve_sentinel = object()

    class _Record:
        curve = curve_sentinel
        calibration_id = "cal-123"

    resolved: list = []

    def resolver(setup, device):
        resolved.append((setup, device))
        return _Record()

    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(resolve_calibration=resolver, meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    geometry = MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0)
    result = _FakeResult(setup={"calibration": {"mode": "serial"}}, device={"label": "UMIK-2"})
    out = analyze(
        program, result, MeasurementPriors(crossover_fc_hz=FC_HZ), geometry,
        phase="verify",
    )

    assert out == "analysis"
    # The resolver was invoked with the capture's setup/device.
    assert resolved == [(result.setup, result.device)]
    # The resolved curve AND the conductor geometry reached the analysis.
    assert seen["calibration"] is curve_sentinel
    assert seen["geometry"] is geometry
    assert seen["geometry"].driver_spacing_m == pytest.approx(0.15)
    assert seen["rate"] == 48000
    # The evidence annotation records the applied calibration.
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": "cal-123",
    }


def test_production_analyze_threads_the_pages_frame_report(monkeypatch):
    """#2094: this seam is the ONLY place both halves of the frame ledger exist.

    The page's account arrives on the capture's authenticated event channel and
    the received count comes out of the WAV this function just decoded, so if
    the report does not cross here it never gets compared to anything — which
    is precisely the state the 2026-08-03 forensics found.
    """
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen.update(capture_report=capture_report, frames=len(samples))
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    report = {"frames": 4, "encoded_frames": 4, "block_gaps": 0,
              "block_gap_frames": 0}
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={},
    )
    analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        _FakeResult(capture_integrity=report),
        MeasurementPriors(crossover_fc_hz=FC_HZ), MeasurementGeometry(),
        phase="verify",
    )
    assert seen["capture_report"] is report

    # And a capture with no report crosses as None, never as an empty dict —
    # "the page said nothing" and "the page said zero" are different facts.
    analyze(
        build_verify_program(FC_HZ, sweep_s=0.5),
        _FakeResult(),
        MeasurementPriors(crossover_fc_hz=FC_HZ), MeasurementGeometry(),
        phase="verify",
    )
    assert seen["capture_report"] is None
    # And the array it counts is the DECODED capture — 4800 frames, not the
    # 9644 bytes of the 16-bit WAV those frames arrived in.
    assert seen["frames"] == 4800


def test_production_analyze_annotates_uncalibrated_when_none_resolves(monkeypatch, caplog):
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen.update(calibration=calibration)
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta=meta
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    # NOT silent: analysis ran uncalibrated, annotated as a stored fact + WARN.
    assert seen["calibration"] is None
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None}
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    # W6.13 round-5 diagnostic: the WARN names what the phone-reported setup
    # actually held at resolve time — here nothing at all.
    assert "setup_mode=absent" in caplog.text


# --- mic_tier threading (#1668 PR-C) --------------------------------------
#
# jasper.audio_measurement.calibration.mic_tier_for_model's own docstring
# names bind_production_analyze as the path that consumes it — this is that
# wiring, and these tests are what pin the claim.


def test_production_analyze_threads_mic_tier_from_resolved_calibration(monkeypatch):
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    class _Record:
        curve = object()
        calibration_id = "cal-umik2"
        model = "minidsp_umik2"

    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: _Record(), meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(setup={"calibration": {"mode": "serial"}})
    incoming_priors = MeasurementPriors(crossover_fc_hz=FC_HZ)
    analyze(
        program, result, incoming_priors, MeasurementGeometry(), phase="verify",
    )

    # The ORIGINAL priors object is untouched (dataclasses.replace returns a
    # new instance) — the mutated copy is what reaches analyze_program_capture.
    assert incoming_priors.mic_tier is None
    assert seen["priors"].mic_tier == "reference"
    assert seen["priors"] is not incoming_priors
    # Every other field survives the replace unchanged.
    assert seen["priors"].crossover_fc_hz == FC_HZ
    # Audit gauntlet 5a: the SAME replace call also threads whether a curve
    # resolved, from the SAME `curve` this function already computed.
    assert incoming_priors.mic_calibrated is None
    assert seen["priors"].mic_calibrated is True


def test_production_analyze_mic_tier_defaults_to_phone_when_no_calibration_resolves(monkeypatch):
    """No calibration record at all (resolver returned None) must resolve
    to the CONSERVATIVE "phone" tier — never a guess at "reference", and
    never a crash on ``getattr(None, "model", None)``."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert seen["priors"].mic_tier == "phone"
    # No calibration resolved: `curve` is None too, and the fact is exactly
    # this (never inferred from the tier, which the next test's bare-curve
    # case would get backwards — a real curve there resolves the SAME
    # conservative "phone" tier while genuinely being calibrated).
    assert seen["priors"].mic_calibrated is False


def test_production_analyze_mic_tier_handles_a_bare_calibration_curve_record(monkeypatch):
    """A record with no ``model`` attribute at all (the "bare
    CalibrationCurve" test-double shape bind_production_analyze already
    special-cases for ``curve``) must not crash — getattr's default takes
    over and resolves to the conservative "phone" tier."""
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.calibration import CalibrationCurve
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen["priors"] = priors
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)
    bare_curve = CalibrationCurve(
        freqs_hz=[20.0, 20000.0], correction_db=[0.0, 0.0],
    )
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: bare_curve, meta={},
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    analyze(
        program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
        MeasurementGeometry(),
        phase="verify",
    )
    assert seen["priors"].mic_tier == "phone"
    # The load-bearing case (audit gauntlet 5a): `mic_tier` alone would read
    # this exactly like the no-calibration-at-all case above — both resolve
    # to "phone" — but a REAL curve WAS applied here, just from a record
    # whose model tier is unrecognized. `mic_calibrated` must not collapse
    # the two: a household with this bare-curve mic must never be told to
    # register one it already has.
    assert seen["priors"].mic_calibrated is True


def test_uncalibrated_warn_reports_the_setup_the_phone_actually_sent(
    monkeypatch, caplog,
):
    """W6.13: the round-5 ambiguity was 'did the phone send NO setup, or a
    setup whose calibration did not resolve?' — the uncalibrated-capture WARN
    now carries the observed calibration mode + id (redacted-safe: never a
    serial or an uploaded file body) so one live journal line settles it."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    monkeypatch.setattr(
        pa_mod, "analyze_program_capture", lambda *a, **k: "analysis"
    )
    analyze = v2host.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta={}
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": "cal-stale",
                "model": "minidsp_umik2",
                "serial": "SECRET-810",
            },
        },
    )
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    assert "setup_mode=stored" in caplog.text
    assert "setup_calibration_id=cal-stale" in caplog.text
    # Redaction: the serial never reaches the journal.
    assert "SECRET-810" not in caplog.text


def test_setup_calibration_observation_is_redacted_safe():
    """The extractor itself: absent / mode-none / stored shapes, and only
    mode + calibration_id ever come back."""
    assert v2host._setup_calibration_observation(None) == ("absent", "")
    assert v2host._setup_calibration_observation({}) == ("absent", "")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "none"}}
    ) == ("none", "")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "stored", "calibration_id": "cal-1"}}
    ) == ("stored", "cal-1")
    assert v2host._setup_calibration_observation(
        {"calibration": {"mode": "serial", "serial": "810-8494"}}
    ) == ("serial", "")


def test_production_analyze_default_resolver_is_the_household_mic_owner():
    """The default resolver IS household_mic.resolve_setup_calibration (the one
    point a capture's setup reference becomes a record) — a no-choice setup
    resolves to None."""
    assert v2host.resolve_setup_calibration(None, None) is None
    assert v2host.resolve_setup_calibration({"calibration": {"mode": "none"}}, None) is None


# --- W6.12: v2 calibration handoff — the household-mic hint reaches a v2 session --
#
# Every v2 capture logged crossover_v2_uncalibrated_capture even with a
# resolvable stored household mic (a UMIK-2 by serial) — root cause: a v2
# capture-plan session has no calibration-picker screen of its own (unlike
# level_ramp/room_sweep) and, unlike the legacy per-driver crossover flow
# (which inherits its choice from the level_ramp page visited first in the
# same tab), never had anywhere to carry the Wave-2 household-mic hint. The
# fix threads correction_setup._default_setup_calibration_for_spec() into
# build_v2_session_spec/build_v2_verify_session_spec (their existing
# **spec_kwargs already forwards to build_crossover_sweep_spec's new
# default_setup_calibration parameter) and applies it silently on the
# capture page. These tests pin each link of that handoff.


def _seed_household_mic(tmp_path, monkeypatch):
    """A resolvable stored household mic (mirrors
    test_default_setup_calibration_for_spec_present_and_absent)."""
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.correction.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )
    return record


def test_default_setup_calibration_for_v2_reuses_the_household_mic_hint(
    tmp_path, monkeypatch,
):
    """No household mic ⇒ no hint (fail-soft); a resolvable one ⇒ the SAME
    hint correction_setup._default_setup_calibration_for_spec builds for
    level_ramp, now available to a v2 session too."""
    assert v2host.default_setup_calibration_for_v2() is None

    record = _seed_household_mic(tmp_path, monkeypatch)

    hint = v2host.default_setup_calibration_for_v2()
    assert hint is not None
    assert hint.mode == "serial"
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is True


def test_v2_session_and_verify_specs_carry_the_default_calibration_hint(
    tmp_path, monkeypatch,
):
    """build_v2_session_spec / build_v2_verify_session_spec's existing
    **spec_kwargs forwards default_setup_calibration through to
    build_crossover_sweep_spec's new parameter, landing on the WIRE spec the
    phone actually receives."""

    record = _seed_household_mic(tmp_path, monkeypatch)
    hint = v2host.default_setup_calibration_for_v2()
    assert hint is not None

    session_spec = build_v2_session_spec(
        _roles(), FC_HZ,
        acknowledgement_binding=_BINDING,
        default_setup_calibration=hint,
    )
    verify_spec = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding=_BINDING, default_setup_calibration=hint,
    )
    for spec in (session_spec, verify_spec):
        wire = spec.to_dict()
        assert wire["default_setup"]["calibration"]["calibration_id"] == (
            record.calibration_id
        )
        assert wire["default_setup"]["calibration"]["mode"] == "serial"

    # Omitted (the pre-W6.12 default): no hint on the wire — every existing
    # caller (including the two legacy correction_setup.py handlers, which
    # never pass this) stays byte-identical.
    bare = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding=_BINDING,
    ).to_dict()
    assert "default_setup" not in bare


def test_plan_flow_stored_calibration_lands_in_the_analyze_call_and_evidence(
    tmp_path, monkeypatch, caplog,
):
    """THE handoff pin: once the capture page applies the household-mic hint
    (a v2 capture posting setup.calibration = {mode: "stored", calibration_id,
    model} — the exact shape applyDefaultCalibrationHintSilently now submits),
    bind_production_analyze's PRODUCTION resolver (resolve_setup_calibration,
    not a mock) must actually apply the calibration curve and record it in the
    persisted evidence — never silently falling back to uncalibrated."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    record = _seed_household_mic(tmp_path, monkeypatch)

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen["calibration"] = calibration
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    meta: dict[str, Any] = {}
    # resolve_calibration defaults to resolve_setup_calibration — the REAL
    # production seam — proving the fix through the exact path a live
    # v2 session rides, not a test double.
    analyze = v2host.bind_production_analyze(meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": record.calibration_id,
                "model": "minidsp_umik2",
            },
        },
        device={"label": "UMIK-2"},
    )
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2"):
        out = analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )

    assert out == "analysis"
    assert seen["calibration"] is not None
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": record.calibration_id,
    }
    assert "crossover_v2_uncalibrated_capture" not in caplog.text


def test_plan_flow_stored_calibration_refuses_on_device_mismatch(
    tmp_path, monkeypatch, caplog,
):
    """The 2026-07-20 incident, through the full production seam: the
    household's UMIK-2 calibration is the resolvable stored default, but THIS
    capture's phone-reported device is a Dayton iMM-6C. The real
    ``resolve_setup_calibration`` seam must refuse to apply it — the
    analysis still runs (never blocked), annotated uncalibrated, with BOTH
    the existing ``crossover_v2_uncalibrated_capture`` WARN and the NEW
    distinct mismatch event."""
    import logging as _logging

    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    record = _seed_household_mic(tmp_path, monkeypatch)

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen["calibration"] = calibration
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    meta: dict[str, Any] = {}
    analyze = v2host.bind_production_analyze(meta=meta)
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    result = _FakeResult(
        setup={
            "calibration": {
                "mode": "stored",
                "calibration_id": record.calibration_id,
                "model": "minidsp_umik2",
            },
        },
        device={"label": "iMM-6C", "device_id": "some-dayton-device-id"},
    )
    with caplog.at_level(_logging.WARNING):
        out = analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )

    assert out == "analysis"
    assert seen["calibration"] is None  # never mis-applied
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None}
    assert "crossover_v2_uncalibrated_capture" in caplog.text
    assert "calibration_device_identity_mismatch" in caplog.text

    # The household record was never re-persisted against the wrong device.
    from jasper.correction.household_mic import read_household_mic
    from jasper.web.correction_setup import _household_mic_path

    saved = read_household_mic(path=_household_mic_path())
    assert saved is not None
    assert saved.model_key == "minidsp_umik2"


# --- status block (S1b) -----------------------------------------------------------


def test_status_block_reports_needs_recovery_and_phase():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "applied": False,
    })
    block = v2status.crossover_v2_status_block()
    assert block["needs_recovery"] is True
    assert block["phase"] == PHASE_MEASURE
    # And the "applying" projection: measure accepted, not yet applied — the
    # conductor's own auto-apply is in flight (owner ruling, 2026-07-20).
    v2host.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_APPLYING


def _linearization_summary(linearization=None, *, outcome=None, analysis=None):
    """``_candidate_summary`` of a measured candidate — the projection itself.

    This is the hop from ``MeasuredCrossoverCandidate`` into the session-scoped
    wizard payload. The envelope suite's own ``_candidate_summary`` is a test
    fixture that starts on the far side of it, so nothing over there can see
    this projection at all.
    """
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
    )

    extra = {}
    if linearization is not None:
        extra["linearization"] = linearization
    if outcome is not None:
        extra["linearization_outcome"] = outcome
    return v2host._candidate_summary(MeasuredCrossoverCandidate(
        program_id="prog-abc",
        analysis=analysis or {
            "alignment_confidence": 0.9, "predicted_ripple_db": 1.1,
            "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4},
        },
        source_preset=_preset(),
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        **extra,
    ))


# The ``_empty_fit`` shape — the envelope allowed correction nowhere — returns
# an EMPTY ``observe_octave_summary`` beside a populated ``reason_summary``, so
# a role can honestly hold verdicts and no numbers.
_VERDICTS_WITHOUT_NUMBERS = {
    "role": "tweeter",
    "observe_octave_summary": {},
    "reason_summary": {"8000": "envelope_out_of_band"},
}


@pytest.mark.parametrize(
    ("linearization", "outcome", "expected", "absent"),
    (
        # Gauge fix items 2/3: only the octave dict is threaded — the rest of
        # ``LinearizationFit.to_dict()``'s cargo stays out of this
        # session-scoped view.
        pytest.param(
            {
                "woofer": {
                    "role": "woofer", "filters": [],
                    "fit_band_hz": [150.0, 3951.5], "target_level_db": -20.22,
                    "residual_rms_db": 4.16, "residual_max_db": 12.21,
                    "reason_summary": {}, "mic_tier": "reference",
                    "driver_class": "unknown", "n_repeats": 2,
                    "observe_octave_summary": {
                        "8000": -0.3, "12000": -1.1, "16000": -2.8,
                    },
                },
                "tweeter": {
                    "role": "tweeter", "filters": [],
                    "fit_band_hz": [2020.0, 13905.2], "target_level_db": -8.63,
                    "residual_rms_db": 2.63, "residual_max_db": 7.13,
                    "reason_summary": {}, "mic_tier": "reference",
                    "driver_class": "unknown", "n_repeats": 2,
                    "observe_octave_summary": {
                        "8000": -0.1, "12000": -3.2, "16000": -9.4,
                    },
                },
            },
            "fitted",
            {
                "linearization_outcome": "fitted",
                "linearization_octaves": {
                    "woofer": {"8000": -0.3, "12000": -1.1, "16000": -2.8},
                    "tweeter": {"8000": -0.1, "12000": -3.2, "16000": -9.4},
                },
            },
            ("filters", "residual_rms_db"),
            id="outcome-and-octaves",
        ),
        # #2638: the number and the verdict travel together or not at all. A
        # stopband octave reads large and POSITIVE, and without its label it
        # reached the review screen as a bare "+23.0 dB".
        pytest.param(
            {
                "woofer": {
                    "role": "woofer",
                    "observe_octave_summary": {"8000": -0.3, "16000": 23.0},
                    "reason_summary": {
                        "8000": "envelope_fitted",
                        "16000": "envelope_out_of_band",
                    },
                },
            },
            "fitted",
            {
                "linearization_octaves": {"woofer": {"8000": -0.3, "16000": 23.0}},
                "linearization_octave_reasons": {
                    "woofer": {
                        "8000": "envelope_fitted",
                        "16000": "envelope_out_of_band",
                    },
                },
            },
            (),
            id="reason-beside-each-octave",
        ),
        # Keying the reasons off the NUMBERS keeps the reason set a subset of
        # the octave set by construction, which is what makes the row above's
        # band-for-band claim true for every candidate rather than most.
        pytest.param(
            {
                "woofer": {
                    "role": "woofer",
                    "observe_octave_summary": {"8000": -0.3},
                    "reason_summary": {"8000": "envelope_fitted"},
                },
                "tweeter": _VERDICTS_WITHOUT_NUMBERS,
            },
            "fitted",
            {
                "linearization_octaves": {"woofer": {"8000": -0.3}},
                "linearization_octave_reasons": {
                    "woofer": {"8000": "envelope_fitted"},
                },
            },
            (),
            id="verdicts-without-numbers-persist-no-reasons",
        ),
        # Audit item 4i: the household remedy for an undeclared class needs the
        # ACTUAL declared class beside the reason, to tell "unknown" (an action
        # exists at /sound/setup/) from a real class's own prior (there is none).
        pytest.param(
            {
                "woofer": {
                    "role": "woofer",
                    "observe_octave_summary": {"8000": -0.3},
                    "reason_summary": {
                        "8000": "envelope_limited_by_class_prior",
                    },
                    "driver_class": "unknown",
                },
            },
            "fitted",
            {"linearization_driver_class": {"woofer": "unknown"}},
            (),
            id="declared-driver-class",
        ),
        # Same subset-of-the-octave-set rule as the reasons, same cause: a
        # class with no octave row to sit beside is never displayed.
        pytest.param(
            {"tweeter": {**_VERDICTS_WITHOUT_NUMBERS, "driver_class": "soft_dome"}},
            "fitted",
            {"linearization_driver_class": {}},
            (),
            id="no-numbers-persists-no-driver-class",
        ),
        pytest.param(
            None,
            None,
            {
                "linearization_outcome": "",
                "linearization_octaves": {},
                "linearization_octave_reasons": {},
                "linearization_driver_class": {},
            },
            (),
            id="defaults-empty",
        ),
    ),
)
def test_candidate_summary_carries_the_linearization_disclosures(
    linearization, outcome, expected, absent,
):
    summary = _linearization_summary(linearization, outcome=outcome)
    for key, value in expected.items():
        assert summary[key] == value, key
    for key in absent:
        assert key not in summary


def test_candidate_summary_carries_whether_the_polarity_was_pinned():
    """The web hop of the basin pin, on the REAL projection (#2607 S3, redux).

    ``_candidate_review_payload`` reads this summary, and the renderer reads
    that payload — so if the bit stops being copied HERE the household row
    silently reverts to "Inverted (measured)" over a polarity an operator
    pinned. The envelope-side guards use their own candidate fixture and are
    structurally blind to this hop: a mutation run that deleted this very line
    left the whole envelope suite green.

    Absent reads False rather than missing, so the renderer's ``=== true`` has
    a value to test on every candidate, including ones frozen before the field.
    """
    pinned = _linearization_summary(analysis={
        "alignment_confidence": 0.9,
        "alignment_objective": "explicit_prescription_committed",
        "polarity_pinned": True,
    })
    assert pinned["polarity_pinned"] is True

    # The same objective WITHOUT the bit — the discriminator the objective
    # cannot supply, which is the whole reason this key exists.
    unpinned = _linearization_summary(analysis={
        "alignment_confidence": 0.9,
        "alignment_objective": "explicit_prescription_committed",
    })
    assert unpinned["polarity_pinned"] is False


def test_candidate_summary_none_candidate_returns_none():
    assert v2host._candidate_summary(None) is None


# --- W6.1 Finding C: session-scoped measurement pause ---------------------------


class _FakeWindow:
    """A recording stand-in for coordinator.measurement_window()."""

    def __init__(self, log: list) -> None:
        self.log = log

    async def __aenter__(self):
        self.log.append("enter")
        return None

    async def __aexit__(self, *exc):
        self.log.append("exit")
        return False


def _patch_measurement_window(monkeypatch, log: list) -> None:
    from jasper import measurement_window as coordinator

    monkeypatch.setattr(
        coordinator, "measurement_window", lambda **kw: _FakeWindow(log)
    )


def test_session_measurement_pause_is_idempotent(monkeypatch):
    """Acquire enters the window exactly once (a second acquire is a no-op, so a
    per-play cannot open a second exclusive window); release exits exactly once
    and a double-release is safe (no double-exit)."""
    log: list = []
    _patch_measurement_window(monkeypatch, log)

    async def scenario():
        assert not v2host.session_measurement_pause_held()
        await v2host.acquire_session_measurement_pause()
        assert v2host.session_measurement_pause_held()
        await v2host.acquire_session_measurement_pause()  # idempotent
        assert v2host.session_measurement_pause_held()
        await v2host.release_session_measurement_pause()
        assert not v2host.session_measurement_pause_held()
        await v2host.release_session_measurement_pause()  # idempotent

    asyncio.run(scenario())
    assert log == ["enter", "exit"]  # exactly one enter, one exit


def test_volume_hooks_hold_pause_from_open_to_every_drain(monkeypatch):
    """The pause is held from volume open through the drain, for BOTH the close
    and abandon paths; a per-play in between (which nest-SKIPS while held) does
    not release it. The failed-open path releases it so voice never strands."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    class _Ctx:
        session_volume_db = -20.0

    for drain in ("close", "abandon"):
        log: list = []
        _patch_measurement_window(monkeypatch, log)
        v2host.reset_session_measurement_pause_for_tests()
        v2host.set_volume_plan_for_tests(SessionVolumePlan())
        cam = _FakeVolCam(-15.0)
        _own_the_fader(monkeypatch, cam)

        async def scenario():
            _sess = _NoGraphSession()
            hooks = v2host._volume_hooks(
                lambda: cam, _Ctx(), tuning=_sess, volume_claim=_sess.claim,
            )
            opened = await hooks.open()
            assert opened is SessionVolumeOpenResult.OPENED
            assert cam.vol == -20.0
            # Held for the whole session; a per-play sees this and skips.
            assert v2host.session_measurement_pause_held()
            await getattr(hooks, drain)()
            assert not v2host.session_measurement_pause_held()
            assert cam.vol == -15.0  # restored

        asyncio.run(scenario())
        assert log == ["enter", "exit"], drain
        v2host.set_volume_plan_for_tests(None)


def test_the_graph_goes_back_before_the_fader_does(monkeypatch):
    """A derived safety property, pinned because it is no longer an accident.

    Before this wave the order was implicit in ``_put_the_graph_back``'s
    position in one function's statement list. Now the graph restore is the
    SESSION's and the fader restore is the plan's, so nothing but this pin
    stops a later edit inverting them — and inverted, the household level lands
    through a still-installed measurement graph, which is the condition an
    un-ducked swap is only safe in the absence of.

    Both halves write into one log, so the assertion is an order and not two
    independent facts.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    order: list[str] = []

    class _LoggingSession:
        def __init__(self) -> None:
            self.claim = _session_claim()
            self.seams = SimpleNamespace(volume=self.claim)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            # The real session restores the graph and THEN releases the claim
            # (reverse order of taking, after W5-c1's setup reorder). Both
            # halves land in the one log, which is what makes this an order.
            order.append("graph")
            if self.seams.volume is not None:
                await self.seams.volume.release()

    class _LoggingCam(_FakeVolCam):
        async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
            order.append("fader")
            return await super().set_volume_db(db, best_effort=best_effort)

    _patch_measurement_window(monkeypatch, [])
    v2host.reset_session_measurement_pause_for_tests()
    v2host.set_volume_plan_for_tests(SessionVolumePlan())

    class _Ctx:
        session_volume_db = -20.0

    cam = _LoggingCam(-15.0)
    _own_the_fader(monkeypatch, cam)

    async def scenario():
        _sess = _LoggingSession()
        hooks = v2host._volume_hooks(
            lambda: cam, _Ctx(), tuning=_sess, volume_claim=_sess.claim,
        )
        await hooks.open()
        order.clear()  # the open's own fader write is not what this pins
        await hooks.close()

    asyncio.run(scenario())
    v2host.set_volume_plan_for_tests(None)

    assert order and order[0] == "graph", (
        f"the fader was restored before the graph went back: {order}"
    )
    assert "fader" in order, "anti-vacuity: the drain really did write the fader"


def test_volume_hooks_release_pause_when_open_does_not_confirm(monkeypatch):
    """If plan.open() drains itself (does not return OPENED), the pause is
    released — a failed open must never leave voice paused with no session."""
    log: list = []
    _patch_measurement_window(monkeypatch, log)
    v2host.reset_session_measurement_pause_for_tests()

    class _DrainedPlan:
        async def open(self, vol, door):
            return "failed"

    # The hooks build their door before calling the plan, and a door needs an
    # owner; production always has one.
    _own_the_fader(monkeypatch, _FakeVolCam(-15.0))

    v2host.set_volume_plan_for_tests(_DrainedPlan())

    class _Ctx:
        session_volume_db = -20.0

    async def scenario():
        _sess = _NoGraphSession()
        hooks = v2host._volume_hooks(
            lambda: _FakeVolCam(-15.0), _Ctx(), tuning=_sess,
            volume_claim=_sess.claim,
        )
        result = await hooks.open()
        assert result == "failed"
        assert not v2host.session_measurement_pause_held()

    asyncio.run(scenario())
    assert log == ["enter", "exit"]


# --- W6.1 Finding E: recovery paths actually recover -----------------------------


def test_reconcile_drains_residual_owned_active_before_new_session(monkeypatch):
    """E1: a residual owned-active plan (a prior failed session's leftover) is
    drained before a fresh session, so plan.open() starts clean instead of
    raising SessionVolumePlanError into the silent 200→adapter_failed loop."""
    from jasper.active_speaker.session_volume_plan import (
        FaderVolumeDoor,
        SessionVolumePlan,
    )

    plan = SessionVolumePlan()
    cam = _FakeVolCam(-15.0)
    _own_the_fader(monkeypatch, cam)
    asyncio.run(plan.open(-20.0, FaderVolumeDoor(cam.set, cam.get)))
    assert plan.measurement_volume_db == -20.0
    assert not plan.needs_recovery  # owned-active this process, within ceiling
    v2host.set_volume_plan_for_tests(plan)

    v2host.reconcile_session_volume_for_new_session(_bg_run_async, lambda: cam)

    assert plan.measurement_volume_db is None  # residual drained
    assert not plan.needs_recovery
    assert cam.vol == -15.0  # restored to household


def test_enforce_ceiling_drains_a_stale_active_and_is_cheap_otherwise(monkeypatch):
    """E3: enforce_ceiling (previously zero callers) force-drains a session that
    outlived the wall-clock ceiling, and is a no-op on a healthy session."""
    from jasper.active_speaker.session_volume_plan import (
        FaderVolumeDoor,
        SessionVolumePlan,
    )

    clock = [1000.0]
    plan = SessionVolumePlan(wall_clock_ceiling_s=10.0, clock=lambda: clock[0])
    cam = _FakeVolCam(-15.0)
    _own_the_fader(monkeypatch, cam)
    asyncio.run(plan.open(-20.0, FaderVolumeDoor(cam.set, cam.get)))
    assert cam.vol == -20.0
    v2host.set_volume_plan_for_tests(plan)

    # Within the ceiling: cheap no-op, nothing drained.
    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is False
    assert plan.measurement_volume_db == -20.0

    # Past the ceiling: force-drained back to the household volume.
    clock[0] = 2000.0
    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True
    assert plan.measurement_volume_db is None
    assert cam.vol == -15.0


# The household-visible conclusion the three drains share, one pin per arm:
# **voice/mux isolation is never freed while a measurement session still owns
# the fader.** Two of the three arms reach _release_pause_best_effort and are
# stopped by its claim gate -- one carrying no outcome at all (the drain
# raised), one carrying LANDED -- which is exactly why the release cannot be
# gated on the outcome. The DEFERRED arm never gets there: it pins the
# ceiling's own early return, the one guard that already existed. Freeing the
# pause on any of them resumes the household programme through a
# crossover-free, role-routed measurement graph.


def test_a_live_claim_holds_the_pause_when_the_ceiling_drain_defers(monkeypatch):
    """Arm 1: the ordinary deferral. The gate still hears the ceiling expired."""
    log: list = []
    _patch_measurement_window(monkeypatch, log)
    plan, cam, _claim, clock = _live_measurement_session(monkeypatch)
    asyncio.run(v2host.acquire_session_measurement_pause())
    clock[0] += 3600.0

    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True, "the caller's gate must still hear that the ceiling expired"

    assert plan.measurement_volume_db == -20.0, "a live session was drained"
    assert cam.vol == -20.0, "the drain moved a fader it does not own"
    assert v2host.session_measurement_pause_held(), (
        "the drain freed the isolation a live session is measuring behind"
    )
    assert log == ["enter"], "the measurement window was exited under the session"
    assert plan.needs_recovery is False, "no recovery screen for a live session"


def test_a_raising_ceiling_drain_holds_the_pause_under_a_live_claim(monkeypatch):
    """Arm 2: the drain RAISES, so there is no outcome to gate on at all.

    ``result`` stays ``None`` — not DEFERRED — so an outcome-gated release
    falls straight through to freeing the pause. The session is still holding
    the fader and still measuring through its graph.
    """
    log: list = []
    _patch_measurement_window(monkeypatch, log)
    plan, cam, _claim, clock = _live_measurement_session(monkeypatch)
    asyncio.run(v2host.acquire_session_measurement_pause())
    clock[0] += 3600.0

    def _raise(*_a, **_kw):
        raise RuntimeError("camilla went away mid-drain")

    monkeypatch.setattr(plan, "enforce_ceiling", _raise)

    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True

    assert v2host.session_measurement_pause_held(), (
        "a raising drain freed the isolation out from under a live session"
    )
    assert log == ["enter"], "the measurement window was exited under the session"


def test_a_same_level_landed_holds_the_pause_under_a_live_claim(monkeypatch):
    """Arm 3: the drain answers LANDED while the claim is still held.

    When the household level already equals the measurement level, the door's
    deferral test (level in effect vs level being restored) does not fire, so
    a LIVE session reads as a completed restore. Reachable on defaults: both
    sides sit at ``MEASUREMENT_REFERENCE_VOLUME_DB`` on a box that never ran
    seat-SPL. No error anywhere — which is what makes an outcome-gated release
    unsafe even on the happy path.
    """
    from jasper.active_speaker.session_volume_plan import (
        MEASUREMENT_REFERENCE_VOLUME_DB,
    )

    log: list = []
    _patch_measurement_window(monkeypatch, log)
    plan, cam, _claim, clock = _live_measurement_session(
        monkeypatch,
        household_db=MEASUREMENT_REFERENCE_VOLUME_DB,
        measurement_db=MEASUREMENT_REFERENCE_VOLUME_DB,
    )
    asyncio.run(v2host.acquire_session_measurement_pause())
    clock[0] += 3600.0

    assert v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True

    # The LANDED branch really was taken -- the plan resolved and cleared its
    # durable intent -- so this is not a deferral wearing a different hat.
    assert plan.measurement_volume_db is None, (
        "expected the LANDED branch; a deferral would leave the intent standing"
    )
    assert v2host.session_measurement_pause_held(), (
        "a coincidental LANDED freed the isolation under a live session"
    )
    assert log == ["enter"], "the measurement window was exited under the session"


def test_recover_on_a_deferral_reports_no_recovery(monkeypatch):
    """Arm 3 of G2: a deferral is not a recovery, and must not be sold as one.

    ``succeeded`` gates both the household's ``recovered`` banner and the
    pause release, so counting DEFERRED as success told the household its
    volume was restored while a live session still held the fader.
    """
    log: list = []
    _patch_measurement_window(monkeypatch, log)
    _plan, cam, _claim, _clock = _live_measurement_session(monkeypatch)
    asyncio.run(v2host.acquire_session_measurement_pause())

    succeeded, recovery = v2host.recover_session_volume(_bg_run_async, lambda: cam)

    assert succeeded is False, "a deferral was reported to the household as recovered"
    assert recovery == v2host.RECOVERY_DEFERRED
    assert v2host.session_measurement_pause_held(), (
        "recover freed the isolation on a restore that has not happened"
    )


def test_the_recovery_deferred_value_tracks_the_enum():
    """``RECOVERY_DEFERRED`` mirrors a value this module cannot import at
    runtime (the plan is a ``TYPE_CHECKING``-only import), so pin them equal."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    assert v2host.RECOVERY_DEFERRED == SessionVolumeRestoreResult.DEFERRED.value


def test_v2_volume_recovery_active_tracks_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2host.set_volume_plan_for_tests(_NeedsRecovery())
    assert v2host.v2_volume_recovery_active() is True

    class _Clean:
        needs_recovery = False

    v2host.set_volume_plan_for_tests(_Clean())
    assert v2host.v2_volume_recovery_active() is False


def test_recover_session_volume_routes_to_the_plan(monkeypatch):
    """E2 host seam: recover_session_volume drains via the v2 plan's
    recover_unresolved (not the legacy lease) and reports the outcome."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    drained: list = []

    class _Plan:
        needs_recovery = True

        async def recover_unresolved(self, door):
            await door.restore_household_level_db(-15.0)
            await door.read_household_level_db()
            drained.append(True)
            return SessionVolumeRestoreResult.EXACT_RESTORED

    v2host.set_volume_plan_for_tests(_Plan())
    cam = _FakeVolCam(-20.0)
    _own_the_fader(monkeypatch, cam)
    succeeded, recovery = v2host.recover_session_volume(_bg_run_async, lambda: cam)
    assert succeeded is True
    assert recovery == "exact_restored"
    assert drained == [True]
    assert cam.vol == -15.0


# --- W6.1 gate should-fix: gate-lease abort under the held window ----------------


def test_gate_abort_mid_play_cancels_the_play_and_names_the_error(monkeypatch):
    """Renew failure mid-play: the coordinator's abort cancels the REGISTERED
    play task (not the session task) and the cancellation surfaces as a named
    MeasurementWindowError so the cleanup arm persists it honestly."""
    from jasper.measurement_window import MeasurementWindowError

    log: list = []
    _patch_measurement_window(monkeypatch, log)

    async def scenario():
        await v2host.acquire_session_measurement_pause()
        target = v2host._session_abort_target
        assert target is not None
        started = asyncio.Event()

        async def play_body():
            started.set()
            await asyncio.sleep(30)

        play = asyncio.create_task(v2host._play_under_session_pause(play_body))
        await started.wait()
        # What the coordinator's refresh task does on a 40 s renew failure.
        target.abort(None)
        with pytest.raises(MeasurementWindowError) as excinfo:
            await play
        assert "isolation was lost" in str(excinfo.value)
        assert target.failed is True

    asyncio.run(scenario())


def test_gate_abort_between_plays_fails_the_next_play_by_name(monkeypatch):
    """Renew failure between plays: the latched failed flag refuses the NEXT
    play with a named error before any audio — never a silent nest-skip into an
    unconfirmed music-isolation gate."""
    from jasper.measurement_window import MeasurementWindowError

    log: list = []
    _patch_measurement_window(monkeypatch, log)
    body_ran: list = []

    async def scenario():
        await v2host.acquire_session_measurement_pause()
        target = v2host._session_abort_target
        target.abort(None)  # no play registered: latch only, no crash

        async def play_body():
            body_ran.append(True)

        with pytest.raises(MeasurementWindowError) as excinfo:
            await v2host._play_under_session_pause(play_body)
        assert "isolation was lost" in str(excinfo.value)

    asyncio.run(scenario())
    assert body_ran == []  # refused before any audio


# --- W6 hardware run 3, finding F: bind_production_play's config_dir SSOT -------


def _probe_bind_production_play_config_dir(monkeypatch, tmp_path) -> dict[str, Any]:
    """Drive ``bind_production_play`` far enough to observe the ``config_dir``
    it threads into its two lock users — ``bind_program_playback_seams`` and,
    since wave 6b, the session measurement graph — short-circuiting via a
    sentinel exception BEFORE any real DSP graph emission/playback, since this
    probe cares only about the config_dir plumbing (graph emission and
    playback have their own coverage elsewhere).

    The graph's lock is captured through ``dsp_writer_lock`` itself rather than
    assumed to match the seams': it is a SECOND lock user threaded from the
    same resolved dir, and a probe that only watched the first would go green
    on a graph locking somewhere else entirely."""
    import contextlib

    from jasper.active_speaker import camilla_yaml as camilla_yaml_mod
    from jasper.active_speaker.crossover_v2 import composition as composition_mod
    from jasper.active_speaker.crossover_v2 import session_graph as session_graph_mod
    import jasper.audio_measurement.program as program_mod
    import jasper.dsp_apply as dsp_apply_mod

    captured: dict[str, Any] = {}

    class _ShortCircuit(Exception):
        pass

    def fake_bind_program_playback_seams(cam, **kwargs):
        captured["config_dir"] = kwargs["config_dir"]
        raise _ShortCircuit("captured config_dir — stop before the DSP plumbing")

    @contextlib.asynccontextmanager
    async def fake_dsp_writer_lock(config_dir, *, source, **_kwargs):
        captured.setdefault("lock_dirs", []).append((source, str(config_dir)))
        yield

    async def _install_taking_only_the_lock(self) -> str:
        # The real emit, where wave 6b put it — so the protection-threading pin
        # below still watches the arguments that reach the emitter — but none
        # of the CamillaDSP transport around it.
        self.graph_yaml()
        async with self._writer_lock():
            pass
        return "probe"

    monkeypatch.setattr(dsp_apply_mod, "dsp_writer_lock", fake_dsp_writer_lock)
    monkeypatch.setattr(
        session_graph_mod.MeasurementSessionGraph,
        "install",
        _install_taking_only_the_lock,
    )
    monkeypatch.setattr(
        composition_mod, "bind_program_playback_seams", fake_bind_program_playback_seams
    )
    _patch_measurement_window(monkeypatch, [])
    protection = {"woofer": (), "tweeter": ()}

    def _emit(*args, **kwargs):
        captured["emitter_protection"] = kwargs["protection_sections_by_role"]
        return "placeholder-graph-yaml"

    monkeypatch.setattr(camilla_yaml_mod, "emit_active_speaker_program_config", _emit)
    monkeypatch.setattr(program_mod, "write_program_wav", lambda path, program: None)

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        capture_session_id="cap_config_dir_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
        protection_sections_by_role=protection,
    )
    with pytest.raises(_ShortCircuit):
        play(PHASE_CHECK, object())
    captured["source_protection"] = protection
    return captured


def test_bind_production_play_default_config_dir_matches_ssot(monkeypatch, tmp_path):
    """W6 hardware run 3 finding F: bind_production_play's config_dir default
    must resolve to the SAME canonical constant every sibling DSP writer
    (commissioning apply/verify, web_commissioning, correction_setup) locks
    against — jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR — not
    the stale "/etc/camilladsp" literal this binding shipped with. An SSOT
    pin: if either side's default drifts away from the other, this fails."""
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

    captured = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)
    assert captured["config_dir"] == str(DEFAULT_CAMILLA_CONFIG_DIR)
    # The session graph is the other lock user, and it locks the same dir under
    # its own source name — so the two DSP writers this binding creates
    # serialize against each other and against every sibling writer.
    assert captured["lock_dirs"] == [
        ("crossover_v2_session_graph", str(DEFAULT_CAMILLA_CONFIG_DIR)),
    ]


def test_bind_production_play_default_config_dir_lock_lands_under_var_lib_camilladsp(
    monkeypatch, tmp_path
):
    """The resolved config_dir's DSP writer lock must land under
    /var/lib/camilladsp — the ONLY tree jasper-correction-web's
    ProtectSystem=full leaves writable (ReadWritePaths=/var/lib/jasper
    /var/lib/camilladsp; see deploy/jasper-correction-web.service). A lock
    under /etc/camilladsp is exactly the EROFS W6 run 3 hit 70 ms into the
    first play."""
    from jasper.dsp_apply import dsp_apply_lock_path

    resolved = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)["config_dir"]
    assert str(dsp_apply_lock_path(resolved)).startswith("/var/lib/camilladsp")


def test_bind_production_play_threads_exact_protection_to_emitter(monkeypatch, tmp_path):
    captured = _probe_bind_production_play_config_dir(monkeypatch, tmp_path)
    assert captured["emitter_protection"] is captured["source_protection"]


# --- Issue #1976: the summed-sweep stimulus must also land at a stable name --


def test_cloud_measure_play_also_persists_canonical_summed_program_wav(
    monkeypatch, tmp_path
):
    """Issue #1976: a measure-stage session that walks the pre-apply cloud
    group (CLOUD_MEASURE) plays the SAME excitation object a literal VERIFY
    capture would — ``program_for_phase`` in crossover_v2_flow.py returns
    ``self._verify_program`` for every phase in ``SUMMED_SWEEP_PHASES`` — but
    a session that never arms PHASE_VERIFY itself used to leave that reusable
    stimulus discoverable only under its cloud-phase filename.

    Confirmed against real bench data
    (``captures/bench-20260730/bundle-d76b55bc6b67``, a measure-stage-only
    session): ``cloud_measure_program.wav`` was on disk, no summed-sweep
    stimulus was recoverable under a predictable name.

    ``_play`` must now ALSO persist ``summed_program.wav`` alongside the
    phase-named file whenever the armed phase is CLOUD_MEASURE, CLOUD_VERIFY,
    or VERIFY itself. This is a NEW, dedicated filename — never
    ``verify_program.wav`` — because corpus-index tooling derives "which
    phases this bundle reached" from which ``{phase}_program.wav`` files
    exist; reusing that name would make a cloud-only bundle false-report
    having reached VERIFY (adversarial-gate SF1, PR #2028)."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    written: list[Path] = []

    def fake_write_program_wav(path, program):
        written.append(Path(path))
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", fake_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        capture_session_id="cap_verify_persist_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    play(PHASE_CLOUD_MEASURE, object())

    session_dir = tmp_path / "crossover_v2" / "cap_verify_persist_probe"
    assert (session_dir / "cloud_measure_program.wav").exists()
    # The phase-named file's presence must stay a reliable phase-reach signal:
    # CLOUD_MEASURE alone must NOT create a verify_program.wav that would make
    # this cloud-only session false-report having reached VERIFY.
    assert not (session_dir / "verify_program.wav").exists()
    assert (session_dir / "summed_program.wav").exists(), (
        "CLOUD_MEASURE must also persist the canonical summed_program.wav — "
        "a session that never arms a literal VERIFY capture would otherwise "
        "leave its reusable stimulus un-replayable offline (#1976)"
    )
    # Written exactly once each — the alias is not re-derived or re-rendered,
    # just a second write of the SAME program object already validated above.
    assert written == [
        session_dir / "cloud_measure_program.wav",
        session_dir / "summed_program.wav",
    ]


def test_verify_play_does_not_overwrite_existing_summed_program_wav(
    monkeypatch, tmp_path
):
    """A CLOUD_VERIFY position captured after the session's own VERIFY anchor
    must not re-render (or clobber) the summed_program.wav VERIFY already
    wrote — the alias write is a fill-if-absent, not an unconditional write,
    so repeated cloud positions in one session cost one extra WAV write, not
    N."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    write_calls: list[Path] = []

    def fake_write_program_wav(path, program):
        write_calls.append(Path(path))
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", fake_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        capture_session_id="cap_verify_no_clobber_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    play(PHASE_VERIFY, object())
    play(PHASE_CLOUD_VERIFY, object())

    session_dir = tmp_path / "crossover_v2" / "cap_verify_no_clobber_probe"
    assert write_calls == [
        session_dir / "verify_program.wav",
        session_dir / "summed_program.wav",
        session_dir / "cloud_verify_program.wav",
    ], (
        "summed_program.wav must be written exactly once, by VERIFY itself "
        "(the phase-named verify_program.wav write, then the summed_program.wav "
        "fill) — CLOUD_VERIFY's fill-if-absent check must skip it"
    )


def test_summed_program_wav_persist_failure_is_best_effort(monkeypatch, tmp_path):
    """A full disk or permissions fault writing the summed_program.wav
    diagnostic copy must never abort the measurement (adversarial-gate SF4,
    PR #2028) — matches the ``bank_take`` convention elsewhere in this module:
    catch, log at WARN, keep going. The phase-named WAV (the file actually
    played) must still have been written before the failure."""
    import jasper.active_speaker.program_playback as program_playback_mod
    import jasper.audio_measurement.program as program_mod

    calls: list[Path] = []

    def flaky_write_program_wav(path, program):
        calls.append(Path(path))
        if Path(path).name == "summed_program.wav":
            raise OSError("ENOSPC: no space left on device")
        Path(path).write_bytes(b"fake-wav-bytes")

    async def fake_verified_program_aplay(bundle_dir, artifact, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(program_mod, "write_program_wav", flaky_write_program_wav)
    monkeypatch.setattr(
        program_playback_mod, "verified_program_aplay", fake_verified_program_aplay
    )
    _patch_measurement_window(monkeypatch, [])

    class _FakeEvidenceStore:
        bundle_dir = tmp_path

        def identify_artifact(self, rel):
            return SimpleNamespace(fingerprint="fake")

    play = v2host.bind_production_play(
        run_async=asyncio.run,
        camilla_factory=lambda: object(),
        evidence_store=_FakeEvidenceStore(),
        capture_session_id="cap_summed_persist_fails_probe",
        topology=object(),
        preset=object(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="hw:Test",
        safety_profile={},
        role_targets={},
        session_volume_db=-20.0,
    )
    # Must not raise — the OSError from the summed_program.wav write is
    # swallowed, not propagated into the measurement.
    play(PHASE_CLOUD_MEASURE, object())

    session_dir = tmp_path / "crossover_v2" / "cap_summed_persist_fails_probe"
    assert (session_dir / "cloud_measure_program.wav").exists(), (
        "the phase-named WAV that was actually played must persist even "
        "when the best-effort diagnostic copy fails"
    )
    assert not (session_dir / "summed_program.wav").exists()


# --- W6 run-6 Blocker M + Finding N: apply's real fingerprint-vocabulary seam ---
#
# Every prior test in this file that reaches "applied" fakes the apply gate
# directly (``observe_apply_success`` called from the phone-driver's
# ``on_deferred`` hook) rather than driving ``handle_v2_apply`` through the
# REAL ``apply_baseline_profile`` guard end to end. That gap is exactly how
# W6 hardware run 6 shipped an apply path that could never succeed: the
# guard compares against ``baseline_candidate_fingerprint`` (the composed
# baseline candidate's own identity), not the MEASURED candidate's
# fingerprint this endpoint reviews with the household — a vocabulary
# mismatch the endpoint tests never caught because they never crossed the
# seam. These tests seed the real topology/design-draft/crossover-preview
# files ``handle_v2_apply``'s real loaders read and drive the actual seam.


class _FakeApplyCam:
    """A CamillaController stand-in for handle_v2_apply's ``camilla_factory``."""

    def __init__(self) -> None:
        self.path: str | None = None

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        self.path = path
        return True

    async def get_config_file_path(self, *, best_effort: bool = False) -> str | None:
        return self.path


def _seed_baseline_apply_environment(monkeypatch, tmp_path):
    """Seed the real topology/design-draft/crossover-preview/measurements
    files ``handle_v2_apply``'s real loaders read (env-var overrides — the
    same pattern as ``tests/test_active_speaker_setup_status.py``), plus the
    baseline-profile/config and DSP-apply state paths. Returns
    ``(topology, preset)`` so a caller can build a ``MeasuredCrossoverCandidate``
    against the exact preset the seam will recompile from the same files.

    W6.11: the crossover-preview file is no longer hand-built and written
    directly — that sidestepped the exact bug this wave fixed (only
    ``/sound/``'s Preview button ever generated it; v2 never did). It is
    produced by ``v2host.ensure_crossover_preview_ready()``, the real
    session-start seam, so this fixture proves the same machinery a browser
    session would drive."""
    from jasper.active_speaker import compile_preset_from_crossover_preview
    from jasper.output_topology import save_output_topology

    from tests.test_active_speaker_baseline_profile import _draft, _dual_apple_topology

    topology = _dual_apple_topology()
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(topology, topology_path)

    draft = _draft(topology)
    draft_path = tmp_path / "design_draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "crossover_preview.json"),
    )
    preview = v2host.ensure_crossover_preview_ready()

    # No driver-test measurements recorded — the run-6 shape: a household
    # applies purely from the reviewed measured candidate.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements_missing.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(tmp_path / "baseline_profile.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )

    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    return topology, preset


def _run6_measured_candidate(preset):
    """A candidate shaped like W6 run 6's evidence (candidate_evidence.json):
    woofer delay 404.777 µs (quantizes to 0.4048 ms), tweeter -13.0327 dB and
    inverted."""
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    return MeasuredCrossoverCandidate(
        program_id=(
            "9579a1bb9e2a3d1d8988670628bdbf6f348de3400e76baa63139abbed5ae0207"
        ),
        analysis={"epsilon_ppm": 29.924, "predicted_ripple_db": 29.6952,
              "alignment_confidence": 0.82,
              "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
        source_preset=preset,
        role_attenuations_db={"tweeter": -13.0327, "woofer": 0.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=404.7770086705022, delay_role="woofer", polarity="invert",
        ),
    )


def _emitted_crossover_filters(cam) -> dict[str, tuple[float, int]]:
    """``{Linkwitz-Riley filter type: (freq, order)}`` from the graph that LOADED.

    Read out of the CamillaDSP config the fake controller was actually handed
    — the far end of the chain this seam keeps honest: declaration written,
    preset recomposed from it, YAML emitted, config loaded. Asserting only
    what ``/sound`` declares proves the first link and takes the other three
    on trust, and "the two agree by construction" is a claim about all four.
    """
    import yaml

    config = yaml.safe_load(Path(cam.path).read_text())
    return {
        str(spec["parameters"]["type"]): (
            float(spec["parameters"]["freq"]), int(spec["parameters"]["order"]),
        )
        for spec in (config.get("filters") or {}).values()
        if str((spec.get("parameters") or {}).get("type", "")).startswith(
            "LinkwitzRiley"
        )
    }


def _apply_issue_ids(payload) -> set[str]:
    """Every issue id an apply payload names, in either shape it names them.

    ``handle_v2_apply`` hands a blocker back as the singular ``issue`` and the
    seam's own list as ``issues``. An assertion that reads only one of the two
    can pass because it looked in the wrong place, which is exactly the
    reassurance a "this guard did NOT fire" test must not give.
    """
    named = [payload.get("issue")] + list(payload.get("issues") or [])
    return {
        str(item.get("id") or item.get("code") or "")
        for item in named
        if isinstance(item, dict)
    }


def _seed_alternative_apply(
    monkeypatch, tmp_path, *, selected_hz=2750.0, selected_order=None,
):
    """A configured draft plus the exact alternative candidate under review.

    **The alternative moves UP, and it has to.** This fixture's tweeter
    declares a protective high-pass floor, and ``handle_v2_apply`` re-checks
    that floor against the candidate BEFORE it writes the declaration. Raising
    a corner can never cross a floor, so 2750 Hz is legal wherever the fixture
    happens to put that floor and stays well under the woofer's 5000 Hz usable
    ceiling. Lowering one walks straight at it: a downward seed would be a
    fixture whose legality is an accident of a number declared in another
    file, and where it is illegal, every test built on it proves nothing
    except that the refusal fires. The downward case gets its own test
    (:func:`test_a_below_floor_apply_is_refused_before_sound_is_written`).

    ``selected_order`` moves the declared SLOPE instead of, or as well as, the
    corner. Sound declares a crossover as three fields and one writer owns all
    three, so a candidate measured at a different order asks the declaration
    to change exactly as a retuned corner does. ``None`` keeps the configured
    order (4, i.e. 24 dB/octave).
    """
    from jasper.active_speaker.design_draft import build_design_draft
    from tests.test_active_speaker_baseline_profile import _draft

    topology, _configured = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    original = _draft(topology)
    manual_candidate = dict(original["driver_research"]["crossover_candidates"][0])
    draft = build_design_draft(
        topology, driver_research=original["driver_research"],
        manual_settings={"drivers": [], "crossover_candidates": [manual_candidate]},
        created_at="2026-08-09T12:00:00Z",
    )
    draft["revision"] = 1
    (tmp_path / "design_draft.json").write_text(
        json.dumps(draft), encoding="utf-8",
    )
    preview = v2host.ensure_crossover_preview_ready()
    from jasper.active_speaker import compile_preset_from_crossover_preview

    configured_preset, issues, _ = compile_preset_from_crossover_preview(
        topology, preview,
    )
    assert configured_preset is not None, issues
    selected_preset = replace(
        configured_preset,
        crossover_regions=tuple(
            replace(
                region,
                # The id embeds the ROUNDED corner, so a slope-only change
                # deliberately keeps the id it already had.
                id=(f"{region.lower_driver}_{region.upper_driver}_"
                    f"{int(round(selected_hz))}hz"),
                fc_hz=selected_hz,
                order=region.order if selected_order is None else selected_order,
            )
            for region in configured_preset.crossover_regions
        ),
    )
    candidate = _run6_measured_candidate(selected_preset)
    v2host.save_v2_state({
        "session_id": "cap_alternative",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "fc_selection": {
            "verdict": "recommend_alternative", "configured_hz": 2500.0,
            "recommended_hz": selected_hz, "comparison_complete": True,
        },
        "sound_design_revision": 1,
        "applied": False,
    })
    return candidate


def test_alternative_apply_saves_sound_then_loads_exact_candidate_once(
    monkeypatch, tmp_path,
):
    from jasper.active_speaker.design_draft import load_design_draft

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    class CountingCam(_FakeApplyCam):
        loads = 0

        async def set_config_file_path(self, path, *, best_effort=False):
            self.loads += 1
            return await super().set_config_file_path(
                path, best_effort=best_effort,
            )

    cam = CountingCam()
    payload = _apply(
        {"expected_candidate_fingerprint": candidate.fingerprint,
         "candidate": candidate.to_dict()},
        _bg_run_async, lambda: cam,
    )

    assert payload["status"] == "applied", payload
    assert cam.loads == 1
    # The declaration is written BEFORE the recompose, which is the whole
    # reason the seam's whole-preset equality guard passes here: a candidate
    # measured at 2750 Hz against a draft still declaring 2500 Hz would be
    # refused ``measured_candidate_preset_mismatch``.
    assert "measured_candidate_preset_mismatch" not in _apply_issue_ids(payload)
    assert load_design_draft()["manual_settings"]["crossover_candidates"][0][
        "frequency_hz"
    ] == 2750.0
    # …and the speaker is PLAYING it. The declaration and the emitted graph
    # agreeing is the whole promise of deriving the write from the candidate.
    assert _emitted_crossover_filters(cam) == {
        "LinkwitzRileyLowpass": (2750.0, 4),
        "LinkwitzRileyHighpass": (2750.0, 4),
    }
    state = v2host.load_v2_state()
    assert state["accepted_sound_revision"] == 2
    assert state["applied"] is True


def test_alternative_apply_saves_sound_and_preview_durably(monkeypatch, tmp_path):
    """#2292 scope 2: accepting an alternative Fc fsyncs FIVE writes at the
    accept/apply seam -- the Sound declaration (apply_measured_crossover_geometry),
    the crossover preview regenerated from it (ensure_crossover_preview_ready),
    the applied baseline profile (persist_applied_baseline_profile),
    observe_apply_success's own v2-state write, and the measured base trim
    that apply banks (driver_base_trim.write_base_trim) -- one file fsync +
    one parent-directory fsync each.

    The fourth pair is #2291's: that write CREATES the rollback anchor
    (``previous_candidate_fingerprint``) and runs after the new graph is already live, so a
    power cut that lost it would leave a corrected speaker with nothing to
    restore to. Counted here rather than merely asserted elsewhere because the
    count is the only thing that can catch the anchor write quietly losing its
    ``durable=True``.

    The fifth pair is the base trim's, durable for the same reason: it and the
    applied profile are two halves of one apply, and a power cut keeping one
    but not the other leaves the box levelling by numbers its graph is not
    playing.
    """
    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))

    payload = _apply(
        {"expected_candidate_fingerprint": candidate.fingerprint,
         "candidate": candidate.to_dict()},
        _bg_run_async, lambda: _FakeApplyCam(),
    )

    assert payload["status"] == "applied", payload
    assert len(fsync_calls) == 10


def test_alternative_blocked_apply_is_honest_and_retry_does_not_resave_sound(
    monkeypatch, tmp_path,
):
    import jasper.active_speaker.baseline_profile as baseline
    import jasper.web.sound_setup as sound

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    saves = 0
    original_save = sound.apply_measured_crossover_geometry

    def counted_save(**kwargs):
        nonlocal saves
        saves += 1
        return original_save(**kwargs)

    async def blocked(*args, **kwargs):
        return {"status": "blocked", "issues": [{
            "severity": "blocker", "code": "forced", "message": "forced",
        }]}

    monkeypatch.setattr(sound, "apply_measured_crossover_geometry", counted_save)
    monkeypatch.setattr(baseline, "apply_baseline_profile", blocked)
    request = {"expected_candidate_fingerprint": candidate.fingerprint,
               "candidate": candidate.to_dict()}

    first = _apply(request, _bg_run_async, _FakeApplyCam)
    second = _apply(request, _bg_run_async, _FakeApplyCam)

    assert first["status"] == second["status"] == "blocked"
    assert "2750 Hz is saved in Sound" in first["error"]
    assert "retry" in second["error"]
    assert saves == 1


def test_alternative_apply_refuses_stale_sound_before_camilla(
    monkeypatch, tmp_path,
):
    from jasper.active_speaker.design_draft import load_design_draft, save_design_draft
    from jasper.output_topology import load_output_topology

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    draft = load_design_draft()
    save_design_draft(
        load_output_topology(), driver_research=draft.get("driver_research"),
        manual_settings=draft.get("manual_settings"),
        operator_inputs={"notes": "changed elsewhere"}, expected_revision=1,
    )

    with pytest.raises(v2host.CrossoverV2Refused, match="Sound changed"):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async,
            lambda: (_ for _ in ()).throw(AssertionError("Camilla touched")),
        )


def test_alternative_stage2_refusal_refuses_before_the_sound_save(
    monkeypatch, tmp_path,
):
    """With Apply clickable on a preflight-refusing box, one click must not
    durably move the Sound declaration: the change arm asserts stage-2
    openability BEFORE ``apply_measured_crossover_geometry``, so the refusal
    arrives raw (the predicate's own sentence, not "saved in Sound") and
    displaces nothing — no declaration write, no Camilla."""
    from jasper.active_speaker.design_draft import load_design_draft

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)

    def refuse_stage_2(_status):
        raise v2host.CrossoverV2Refused("the safety declaration changed")

    monkeypatch.setattr(v2host, "_assert_stage_2_can_open", refuse_stage_2)
    with pytest.raises(
        v2host.CrossoverV2Refused, match="the safety declaration changed",
    ):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async,
            lambda: (_ for _ in ()).throw(AssertionError("Camilla touched")),
        )

    assert load_design_draft()["revision"] == 1
    assert "accepted_sound_revision" not in (v2host.load_v2_state() or {})


def test_alternative_camilla_failure_reports_sound_saved_and_allows_retry(
    monkeypatch, tmp_path,
):
    candidate = _seed_alternative_apply(monkeypatch, tmp_path)

    def unavailable_camilla():
        raise RuntimeError("Camilla unavailable")

    with pytest.raises(
        v2host.CrossoverV2Refused, match="2750 Hz is saved in Sound but was not applied",
    ):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async, unavailable_camilla,
        )

    assert (v2host.load_v2_state() or {})["accepted_sound_revision"] == 2


def test_alternative_apply_failed_is_non_success_and_retry_does_not_resave(
    monkeypatch, tmp_path,
):
    import jasper.active_speaker.baseline_profile as baseline
    import jasper.web.sound_setup as sound

    # A fractional corner on purpose: the refusal copy runs every frequency
    # through one formatter, and a value that rounds to a different integer is
    # the only thing that catches a caller reaching past it.
    candidate = _seed_alternative_apply(monkeypatch, tmp_path, selected_hz=2750.6)
    saves = 0
    original_save = sound.apply_measured_crossover_geometry

    def counted_save(**kwargs):
        nonlocal saves
        saves += 1
        return original_save(**kwargs)

    async def apply_failed(*args, **kwargs):
        return {"status": "apply_failed", "apply": {
            "phase": "load", "result": "load_failed_rolled_back", "finished_at": "done",
            "rollback_attempted": True, "rollback_succeeded": True,
        }, "issues": [{
            "severity": "blocker", "code": "load_failed", "message": "no load",
        }]}

    monkeypatch.setattr(sound, "apply_measured_crossover_geometry", counted_save)
    monkeypatch.setattr(baseline, "apply_baseline_profile", apply_failed)
    request = {"expected_candidate_fingerprint": candidate.fingerprint,
               "candidate": candidate.to_dict()}

    for _attempt in range(2):
        with pytest.raises(
            v2host.CrossoverV2Refused,
            match="2750.6 Hz is saved in Sound but was not applied",
        ):
            _apply(request, _bg_run_async, _FakeApplyCam)

    state = v2host.load_v2_state() or {}
    assert saves == 1
    assert state["accepted_sound_revision"] == 2
    assert state["apply_blocked"]["id"] == "load_failed"


@pytest.mark.parametrize("apply_state", [
    None,
    {"phase": "confirm", "result": "confirm_failed_rolled_back",
     "finished_at": "done",
     "rollback_attempted": True, "rollback_succeeded": False},
    {"phase": "prepare", "result": "prepare_failed"},
    {"phase": "confirm", "result": "confirm_failed",
     "rollback_attempted": False, "rollback_succeeded": None},
])
def test_unproven_apply_failure_reports_unknown_current_dsp(
    monkeypatch, tmp_path, apply_state,
):
    import jasper.active_speaker.baseline_profile as baseline

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)

    async def apply_failed(*args, **kwargs):
        payload = {"status": "apply_failed", "issues": [{
            "severity": "blocker", "code": "load_failed", "message": "no load",
        }]}
        if apply_state is not None:
            payload["apply"] = apply_state
        return payload

    monkeypatch.setattr(baseline, "apply_baseline_profile", apply_failed)
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async, _FakeApplyCam,
        )

    assert "could not confirm whether DSP apply finished" in str(excinfo.value)
    assert "retry this same action" not in str(excinfo.value)
    state = v2host.load_v2_state() or {}
    assert state["accepted_sound_revision"] == 2
    assert state["apply_blocked"]["id"] == "apply_result_unknown"


@pytest.mark.parametrize("outcome", ["returned", "raised", "applied"])
def test_post_dsp_outcome_cannot_contaminate_a_replacement_review(
    monkeypatch, tmp_path, outcome,
):
    import jasper.active_speaker.baseline_profile as baseline

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)

    async def stale_outcome(*args, **kwargs):
        v2host.save_v2_state({
            "session_id": "replacement", "accepted_phases": [PHASE_MEASURE],
            "candidate": {"fingerprint": "replacement-fp"}, "applied": False,
        })
        if outcome == "raised":
            raise RuntimeError("connection dropped during load")
        if outcome == "applied":
            return {"status": "applied", "profile": {}}
        return {"status": "apply_failed", "issues": []}

    monkeypatch.setattr(baseline, "apply_baseline_profile", stale_outcome)
    monkeypatch.setattr(baseline, "applied_program_level_delta_db", lambda *a: 0.0)
    if outcome == "applied":
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async, _FakeApplyCam,
        )
    else:
        with pytest.raises(v2host.CrossoverV2Refused, match="could not confirm"):
            _apply(
                {"expected_candidate_fingerprint": candidate.fingerprint,
                 "candidate": candidate.to_dict()},
                _bg_run_async, _FakeApplyCam,
            )

    state = v2host.load_v2_state() or {}
    assert state["session_id"] == "replacement"
    assert state["candidate"]["fingerprint"] == "replacement-fp"
    assert "apply_blocked" not in state
    assert state["applied"] is False


def test_sound_change_during_preflight_refuses_before_camilla(
    monkeypatch, tmp_path,
):
    from jasper.active_speaker.design_draft import load_design_draft, save_design_draft
    from jasper.output_topology import load_output_topology

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    calls = 0

    def change_sound(_status):
        # First call is the change arm's pre-save assert; the subject here is
        # the AT-COMMIT one (D3), so the mutation lands on the second call.
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        draft = load_design_draft()
        save_design_draft(
            load_output_topology(), driver_research=draft.get("driver_research"),
            manual_settings=draft.get("manual_settings"),
            operator_inputs={"notes": "changed during preflight"},
            expected_revision=draft["revision"],
        )

    monkeypatch.setattr(v2host, "_assert_stage_2_can_open", change_sound)
    with pytest.raises(v2host.CrossoverV2Refused, match="Sound changed after"):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async,
            lambda: (_ for _ in ()).throw(AssertionError("Camilla touched")),
        )


def test_alternative_apply_exception_records_an_unknown_dsp_result(
    monkeypatch, tmp_path,
):
    import jasper.active_speaker.baseline_profile as baseline

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)

    async def uncertain(*args, **kwargs):
        raise RuntimeError("connection dropped during load")

    monkeypatch.setattr(baseline, "apply_baseline_profile", uncertain)
    with pytest.raises(
        v2host.CrossoverV2Refused, match="could not confirm whether DSP apply finished",
    ):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async, _FakeApplyCam,
        )

    state = v2host.load_v2_state() or {}
    assert state["accepted_sound_revision"] == 2
    assert state["apply_blocked"]["id"] == "apply_result_unknown"


def test_alternative_sound_save_cannot_mark_a_replacement_review(
    monkeypatch, tmp_path,
):
    import jasper.web.sound_setup as sound

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    original_save = sound.apply_measured_crossover_geometry

    def save_then_replace_review(**kwargs):
        saved = original_save(**kwargs)
        v2host.save_v2_state({
            "session_id": "fresh-review",
            "candidate": {"fingerprint": "fresh-fingerprint"},
            "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        })
        return saved

    monkeypatch.setattr(
        sound, "apply_measured_crossover_geometry", save_then_replace_review,
    )
    with pytest.raises(v2host.CrossoverV2Refused, match="review was replaced"):
        _apply(
            {"expected_candidate_fingerprint": candidate.fingerprint,
             "candidate": candidate.to_dict()},
            _bg_run_async,
            lambda: (_ for _ in ()).throw(AssertionError("Camilla touched")),
        )

    state = v2host.load_v2_state() or {}
    assert state["session_id"] == "fresh-review"
    assert state["candidate"]["fingerprint"] == "fresh-fingerprint"
    assert "accepted_sound_revision" not in state


def test_a_below_floor_apply_is_refused_before_sound_is_written(
    monkeypatch, tmp_path, caplog,
):
    """A refused apply must displace NOTHING — and the only way to promise
    that is to refuse before the durable write, not after it.

    The apply saves the Sound declaration first because the seam's whole-preset
    equality guard demands the declaration already carry the candidate's
    crossover. So the L0 emit gate, which refuses this same below-floor
    condition, can only refuse once ``/sound`` has already been moved: the
    graph is correctly rejected and the household is left with a declaration
    naming a crossover the speaker is not playing and cannot be made to play.
    The failure this pins is that ordering, not the refusal — a boundary check
    that ran one line later would still raise, and would still be broken.

    **1500 Hz is chosen, not tidied.** It sits below any declared floor this
    fixture's tweeter has carried, so the pin survives the floor moving in the
    file that owns it; a value nearer the configured 2500 Hz corner would stop
    being below the floor the next time that number is retuned, and this test
    would then pass by measuring nothing. Nothing here asserts what the floor
    IS — only that a refusal names one and says how to clear it.
    """
    from jasper.active_speaker.design_draft import load_design_draft

    candidate = _seed_alternative_apply(monkeypatch, tmp_path, selected_hz=1500.0)

    with caplog.at_level(logging.INFO):
        with pytest.raises(
            v2host.CrossoverV2Refused,
            match=(
                "it crosses at 1500 Hz, below the tweeter's own declared "
                "protective high-pass floor of"
            ),
        ) as excinfo:
            _apply(
                {"expected_candidate_fingerprint": candidate.fingerprint,
                 "candidate": candidate.to_dict()},
                _bg_run_async,
                lambda: (_ for _ in ()).throw(AssertionError("Camilla touched")),
            )

    # The household is told what to do about it, not merely that it failed.
    assert "raise the crossover to at least" in str(excinfo.value)
    # The machine-readable half. The sentence above may be reworded; this slug
    # is what an operator greps a hearing-safety refusal out of the journal by.
    assert "event=correction.crossover_v2_apply_refused" in caplog.text
    assert "reason=crossover_below_declared_protection_floor" in caplog.text

    draft = load_design_draft()
    assert draft["manual_settings"]["crossover_candidates"][0][
        "frequency_hz"
    ] == 2500
    # Not one write happened: the revision the seed left is still the live one,
    # so there is nothing for a household to undo and nothing for the next
    # measurement session to read as its configured crossover.
    assert draft["revision"] == 1
    state = v2host.load_v2_state() or {}
    assert state.get("accepted_sound_revision") is None
    assert state["applied"] is False


def test_a_persisted_fc_selection_no_longer_decides_what_sound_is_told(
    monkeypatch, tmp_path,
):
    """The apply is fed by the CANDIDATE, never by a record that claims
    something about it.

    A persisted ``fc_selection`` is advisory evidence the review screen renders
    — and while it was the gate, the two could disagree: a stale
    ``recommend_alternative`` from an earlier sweep made an apply write a
    crossover into ``/sound`` that the candidate about to be emitted was never
    measured at. Here the candidate crosses exactly where Sound already
    declares, so the honest answer is "write nothing", and a fully-formed
    contrary record must not change it.
    """
    from jasper.active_speaker import compile_preset_from_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.output_topology import load_output_topology

    _seed_alternative_apply(monkeypatch, tmp_path)
    # Recompile the candidate from what /sound DECLARES, so this apply asks for
    # no declaration change at all.
    preview = v2host.ensure_crossover_preview_ready()
    configured_preset, issues, _gates = compile_preset_from_crossover_preview(
        load_output_topology(), preview,
    )
    assert configured_preset is not None, issues
    as_declared = _run6_measured_candidate(configured_preset)
    v2host.save_v2_state({
        "session_id": "cap_stale_selection",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": as_declared.fingerprint},
        # Everything the old gate needed to fire, all of it true of some other
        # review: a complete comparison recommending a different crossover.
        "fc_selection": {
            "verdict": "recommend_alternative", "configured_hz": 2500.0,
            "recommended_hz": 2750.0, "comparison_complete": True,
        },
        "sound_design_revision": 1,
        "applied": False,
    })

    payload = _apply(
        {"expected_candidate_fingerprint": as_declared.fingerprint,
         "candidate": as_declared.to_dict()},
        _bg_run_async, _FakeApplyCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    draft = load_design_draft()
    assert draft["manual_settings"]["crossover_candidates"][0][
        "frequency_hz"
    ] == 2500
    # Byte-for-byte the pre-seam behaviour: an as-declared apply writes Sound
    # nothing, so the revision never moves and there is no inverse to record.
    assert draft["revision"] == 1
    state = v2host.load_v2_state() or {}
    assert state.get("accepted_sound_revision") is None
    # The control that keeps this test honest: the contrary record was STILL
    # there while the apply ran. A refactor that cleared it earlier would make
    # every assertion above pass for a reason this test is not about.
    assert state["fc_selection"]["verdict"] == "recommend_alternative"


def test_a_slope_only_change_reaches_the_declaration_and_leaves_fc_alone(
    monkeypatch, tmp_path,
):
    """A candidate measured at a different SLOPE moves ``/sound`` too.

    The declaration states three fields and the seam's staleness guard is a
    whole-preset equality, so a candidate measured at 12 dB/octave against a
    draft declaring 24 is exactly as unapplyable as one measured at a different
    corner. While the writer carried only the frequency, this apply could ONLY
    be refused ``measured_candidate_preset_mismatch``: nothing could make the
    declaration come to agree with it.

    The corner is asserted UNCHANGED beside the slope, for the opposite
    hazard — a writer that moves a field the change never named leaves the
    same disagreement, pointing the other way.
    """
    from jasper.active_speaker.design_draft import load_design_draft

    # Same corner, half the slope: order 2 is 12 dB/octave, against the 24
    # dB/octave (order 4) the draft declares.
    candidate = _seed_alternative_apply(
        monkeypatch, tmp_path, selected_hz=2500.0, selected_order=2,
    )
    regions = candidate.source_preset.crossover_regions
    # The region id embeds the rounded corner, so a slope-only change keeps the
    # id it had — asserted here because a changed id would make this test pass
    # for the wrong reason (a whole different region, not a resloped one).
    assert [region.id for region in regions] == ["woofer_tweeter_2500hz"]

    cam = _FakeApplyCam()
    payload = _apply(
        {"expected_candidate_fingerprint": candidate.fingerprint,
         "candidate": candidate.to_dict()},
        _bg_run_async, lambda: cam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    # The declaration is written BEFORE the recompose precisely so this guard
    # passes; a slope left out of that write would trip it.
    assert "measured_candidate_preset_mismatch" not in _apply_issue_ids(payload)
    declared = load_design_draft()["manual_settings"]["crossover_candidates"][0]
    assert declared["slope_db_per_octave"] == 12.0
    assert declared["frequency_hz"] == 2500.0
    assert declared["filter_type"] == "Linkwitz-Riley"
    # The emitted graph carries the measured slope and the corner nobody moved.
    assert _emitted_crossover_filters(cam) == {
        "LinkwitzRileyLowpass": (2500.0, 2),
        "LinkwitzRileyHighpass": (2500.0, 2),
    }


def test_apply_translates_measured_fingerprint_to_baseline_fingerprint(
    monkeypatch, tmp_path,
):
    """Blocker M, positive: drives handle_v2_apply through the REAL
    apply_baseline_profile guard end to end (no faked apply gate) with a
    run-6-shaped measured candidate, and asserts the guard passes and the
    emitted config carries the measured delay + inversion."""
    from jasper.active_speaker.baseline_profile import baseline_candidate_fingerprint

    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    corrections = payload["profile"]["corrections"]
    assert corrections["woofer"]["delay_ms"] == pytest.approx(0.4048, abs=1e-4)
    assert corrections["woofer"]["inverted"] is False
    assert corrections["tweeter"]["delay_ms"] == 0.0
    assert corrections["tweeter"]["gain_db"] == pytest.approx(-13.0327, abs=1e-4)
    assert corrections["tweeter"]["inverted"] is True
    config_text = (tmp_path / "active_speaker_baseline.yml").read_text(
        encoding="utf-8"
    )
    assert "delay: 0.4048" in config_text

    # The fingerprint that actually reached the seam is the COMPOSED baseline
    # candidate's own identity, never the measured candidate's fingerprint —
    # confirming the vocabulary translation happened rather than the two
    # values accidentally colliding.
    assert payload["profile"]["candidate_fingerprint"] != candidate.fingerprint
    assert payload["profile"][
        "candidate_fingerprint"
    ] == baseline_candidate_fingerprint(payload["profile"])

    # Success arms the deferred VERIFY gate and clears any stale apply-blocked
    # nudge (Finding N).
    assert v2host._applied_gate() is True
    saved_state = v2host.load_v2_state()
    assert saved_state["apply_blocked"] is None


# --- #1811: the apply boundary declares its level move, and moves no level ------


class _FakeApplyAndVolumeCam(_FakeApplyCam):
    """``_FakeApplyCam`` plus the main-volume RPCs the session plan drives, so
    ONE ``camilla_factory`` can both apply and hold the session volume — which
    is what lets these tests assert the commanded level did NOT move."""

    vol = -20.0

    async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
        type(self).vol = float(db)
        return True

    async def get_volume_db(self, best_effort: bool = False) -> float:
        return type(self).vol


# The offset the apply declares for ``_boosting_candidate(boost_db=6.0)``:
# the PEAK-rule charge (#1808) for a +6 dB bell at 900 Hz, which sits inside
# the woofer's own passband so the crossover credits back almost nothing while
# the headroom margin adds ~1 dB. Rounded to 3 dp by the persist path.
_APPLY_OFFSET_DB = -6.86


def _boosting_candidate(preset, *, boost_db: float):
    """The run-6 candidate plus a Layer-1a boost — the shape that charges
    program headroom and therefore moves the chain at apply time."""
    return replace(
        _run6_measured_candidate(preset),
        linearization={
            "woofer": {
                "filters": [
                    {
                        "biquad_type": "Peaking",
                        "freq": 900.0,
                        "q": 3.0,
                        "gain": boost_db,
                    },
                ],
                "headroom_cost_db": boost_db,
            },
        },
        linearization_outcome="fitted",
    )


def _open_session_volume_plan(*, household_db: float, measurement_db: float = -20.0):
    """An OPEN plan holding ``measurement_db``, as a live session would."""
    from jasper.active_speaker.session_volume_plan import (
        FaderVolumeDoor,
        SessionVolumeOpenResult,
        SessionVolumePlan,
    )

    _FakeApplyAndVolumeCam.vol = household_db
    plan = SessionVolumePlan()
    cam = _FakeApplyAndVolumeCam()
    assert (
        asyncio.run(plan.open(measurement_db, FaderVolumeDoor(cam.set_volume_db, cam.get_volume_db)))
        is SessionVolumeOpenResult.OPENED
    )
    assert _FakeApplyAndVolumeCam.vol == measurement_db
    v2host.set_volume_plan_for_tests(plan)
    return plan


def test_apply_declares_its_level_move_and_never_touches_the_volume(
    monkeypatch, tmp_path,
):
    """#1811, through the REAL apply seam.

    The applied graph absorbs its correction's boost as a pre-split common
    attenuation, so the same commanded volume drives the speaker quieter the
    instant the config swaps. That absorption is the excitation-safety property
    (``camilla_yaml``: the boosted band lands "at or under unity no matter how
    deep the correction"), so the apply must **declare** the move for the
    analysis and must **not** compensate it at the main volume — compensating
    would put the boosted band over the driver's excitation cap by the
    branch's own boost.

    Three things must hold:

    * the declared offset is the emitter's OWN delta (here −6 dB, read off the
      applied profile, not a constant);
    * the commanded session volume is completely untouched;
    * it is durable BEFORE ``observe_apply_success`` returns — that call sets
      the ``applied`` flag which releases VERIFY's deferred hold, and the
      probe seam reads the offset off the same state one capture later.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    plan = _open_session_volume_plan(household_db=-6.0)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    # The PEAK-rule charge (#1808) for a +6 dB bell at 900 Hz — inside the
    # woofer's own passband, so the crossover credits back almost nothing and
    # the headroom margin adds ~1 dB on top. Verified by running, not derived
    # here: what this test pins is that the DECLARED number is the emitter's
    # own, whatever the charge rule of the day makes it.
    assert payload["expected_post_apply_offset_db"] == _APPLY_OFFSET_DB
    # Durable, and readable through the very seam the conductor's probe uses.
    assert v2host.load_v2_state()["expected_post_apply_offset_db"] == _APPLY_OFFSET_DB
    assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB
    # The speaker's commanded level did not move. This is the safety claim.
    assert _FakeApplyAndVolumeCam.vol == -20.0
    assert plan.measurement_volume_db == -20.0


def test_a_blocked_apply_declares_no_offset_and_moves_no_level(monkeypatch, tmp_path):
    """An apply the seam refused changed no graph, so there is no move to
    declare — and the probe seam must keep reporting "nothing known" (0.0)
    rather than an offset from a transaction that never landed."""
    from jasper.active_speaker.design_draft import build_design_draft

    from tests.test_active_speaker_baseline_profile import _research

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    plan = _open_session_volume_plan(household_db=-6.0)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    # Move the crossover design out from under the reviewed candidate, exactly
    # as the preset-mismatch test above does, so the seam blocks.
    moved_research = _research()
    moved_research["crossover_candidates"][0]["frequency_hz"] = 3000
    (tmp_path / "design_draft.json").write_text(
        json.dumps(
            build_design_draft(
                topology,
                driver_research=moved_research,
                created_at="2026-07-18T12:30:00Z",
            )
        ),
        encoding="utf-8",
    )
    v2host.ensure_crossover_preview_ready()

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )

    assert payload["status"] == "blocked"
    assert "expected_post_apply_offset_db" not in payload
    assert v2host._applied_offset_gate() == 0.0
    assert _FakeApplyAndVolumeCam.vol == -20.0
    assert plan.measurement_volume_db == -20.0


def test_the_declared_offset_survives_persist_conductor_state(monkeypatch, tmp_path):
    """The durable seam BETWEEN the writer and the reader (#1811 blocker).

    ``observe_apply_success`` writes the offset and the probe's seam reads it,
    and both halves were pinned — but nothing crossed the
    ``persist_conductor_state`` call that happens on every capture in between.
    It rebuilds the state from a fresh dict literal, so the offset was erased
    on every single call while ``applied`` survived: the CLOUD_VERIFY probe
    (the one with the spatial arm AND rollback authority) would have graded
    the apply's own headroom charge blind and could roll a healthy correction
    back, and every "Try again" re-arm — which persists under a brand-new
    session id — would have been blind too.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _boosting_candidate(preset, boost_db=6.0)
    _open_session_volume_plan(household_db=-6.0)
    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyAndVolumeCam,
    )
    assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB

    # One more capture in the SAME session, then the re-arm's brand-new one.
    for session_id in ("cap_run6", "cap_rearm"):
        v2host.persist_conductor_state(
            _StubConductor(session_id), failure_code=None,
        )
        assert v2host._applied_offset_gate() == _APPLY_OFFSET_DB, session_id


class _StubConductor:
    """The minimum ``persist_conductor_state`` reads off a conductor."""

    candidate = None
    verify_outcome = None
    verify_code = None
    verify_gate = None
    verify_evidence = None
    verify_graded_band_hz = None
    verify_frame = None
    verify_claims = None
    delta_probe = None
    measure_predicted_sum = None
    measure_gate_window_ms = None
    verify_pilot_transfer_reference = None
    verify_level_reference_reset = None
    session_phases: tuple = ()

    def __init__(
        self, session_id: str = "s1", *, applied: bool = True,
        session_phases: tuple = (),
    ) -> None:
        self._session_id = session_id
        self._applied = applied
        self._session_phases = session_phases

    def snapshot(self):
        return SimpleNamespace(
            session_id=self._session_id, accepted_phases=(),
            session_phases=self._session_phases,
            tier="", applied=self._applied, gain_plan_db=None,
            candidate_fingerprint=None, cloud_close="",
        )


def test_only_verify_rebind_carries_an_accepted_sound_revision():
    v2host.save_v2_state({"session_id": "old", "accepted_sound_revision": 4})
    v2host.persist_conductor_state(_StubConductor("verify"), failure_code=None)
    assert (v2host.load_v2_state() or {})["accepted_sound_revision"] == 4

    v2host.persist_conductor_state(
        _StubConductor("measure", session_phases=(PHASE_CHECK, PHASE_MEASURE)),
        failure_code=None,
    )
    assert (v2host.load_v2_state() or {})["accepted_sound_revision"] is None


def test_every_host_owned_apply_key_survives_persist_conductor_state():
    """The drift guard for a bug class that has now shipped THREE times.

    ``persist_conductor_state`` rebuilds the durable state from a fresh dict
    literal, so any key whose value comes from ``observe_apply_success`` —
    which the conductor neither produces nor reads — is erased unless a
    carry-forward line exists for it. That has been a P0 for
    the way-back stash (W6.12), for ``cloud`` (PR-4 B1), and for
    ``expected_post_apply_offset_db`` (#1811).

    The host-owned set is derived MECHANICALLY rather than listed: a key is
    host-owned when the apply path gives it a value and a persist driven by
    the conductor ALONE (empty prior, nothing to carry) cannot regenerate one.
    A fourth such key fails this test the moment it is written, without anyone
    having to remember to extend a list.

    **If your new key legitimately wants session scoping** — ``apply_blocked``
    does, because a blocked apply refuses the deferred VERIFY outright and so
    never faces a new-session rebind — put it in ``persist_conductor_state``'s
    session-gated branch and add an exception here WITH that reason. Reaching
    for the exception first is the mistake this guard exists to make visible.

    The re-arm's brand-new session id is the hard case, and the one all three
    bugs hit, so that is what this crosses.
    """
    # (1) What a persist can rebuild from the conductor alone, with an empty
    # prior so nothing can be carried forward.
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(_StubConductor("s1"), failure_code=None)
    from_conductor_alone = {
        key for key, value in (v2host.load_v2_state() or {}).items()
        if value is not None
    }

    # (2) What the apply path establishes on top of it.
    v2host.save_v2_state({
        "session_id": "s1", "applied": False,
        "accepted_phases": [PHASE_MEASURE], "candidate": {"fingerprint": "fp"},
    })
    v2host.observe_apply_success(
        "fp",
        previous_candidate_fingerprint="fp-prior-measured",
        expected_post_apply_offset_db=-22.458,
    )
    after_apply = dict(v2host.load_v2_state() or {})
    host_owned = {
        key for key, value in after_apply.items()
        if value is not None and key not in from_conductor_alone
    }
    # The derivation must actually see the class's keys — a guard that derives
    # an empty set proves nothing.
    assert "expected_post_apply_offset_db" in host_owned
    assert "previous_candidate_fingerprint" in host_owned
    # The way-back pointer's pairing — the automatic revert's arming fact.
    assert "previous_candidate_displaced_by" in host_owned

    # (3) Cross the seam under the re-arm's BRAND-NEW session id.
    v2host.persist_conductor_state(_StubConductor("cap_rearm"), failure_code=None)
    after_persist = v2host.load_v2_state() or {}
    for key in sorted(host_owned):
        assert after_persist.get(key) == after_apply[key], (
            f"{key!r} is written by the apply path and erased by "
            "persist_conductor_state — add a carry-forward line for it"
        )


# Absent is what "unknown" looks like on every field of this summary (#2533);
# ``safety_anchored`` is False because the realized-energy check cannot have run
# on a probe that does not carry the field (series-2 D1).
_UNKNOWN_DELTA_PROBE_FIELDS = {
    "safety_anchored": False,
    "entry_anchor_offset_db": None,
    "quiet_n_bins": None,
    "quiet_core_band_hz": None,
    "quiet_probe_coverage": None,
}

# Two of the four are structurally not-evaluated: VERIFY plays one summed
# sweep, so no per-branch capture exists to grade (R18, #1868).
_SECTION_7_CLAIMS = {
    "woofer_branch": {
        "status": "not_evaluated", "reason": "no_per_branch_verify_capture",
    },
    "hf_branch": {
        "status": "not_evaluated", "reason": "no_per_branch_verify_capture",
    },
    "integration": {"status": "pass", "max_db": 0.069, "tolerance_db": 1.5},
    "absolute": {
        "status": "pass", "max_db": 0.69, "tolerance_db": 2.0,
        "band_hz": [1000.0, 4000.0], "worst_db": 0.69, "worst_hz": 1050.0,
    },
}

_COMPARED_FRAME = {
    "offset_db": -0.75, "tilt_db_per_octave": -0.79,
    "rms_db_tilt_removed": 1.34, "max_db_tilt_removed": 0.62,
}

# Copied byte-for-byte off the conductor — the host does not re-derive a
# sentence of its own (#1966).
_GATE_DISCLOSURE = {
    "disclosure": (
        "no reflection found; window capped at the 7.00 ms search ceiling, "
        "so nothing was gated out, valid above 357 Hz"
    ),
    "reflection_measured": False,
}


@pytest.mark.parametrize(
    ("attrs", "key", "expected", "also_absent", "plain_outcome"),
    (
        # #1811 SF1. A ``level_mismatch`` produces no refusal, so it only ever
        # occurs alongside a pass — gating this on failure persists it never.
        pytest.param(
            {"delta_probe": SimpleNamespace(
                verdict="level_mismatch", reason="uncommanded_level_shift",
                expected_offset_db=-22.458, residual_offset_db=-4.0,
                frame=SimpleNamespace(
                    offset_db=None, tilt_db_per_octave=None, n_bins=0,
                    band_hz=None,
                ),
            )},
            "delta_probe",
            {
                **_UNKNOWN_DELTA_PROBE_FIELDS,
                "verdict": "level_mismatch",
                "reason": "uncommanded_level_shift",
                "expected_offset_db": -22.458, "residual_offset_db": -4.0,
                "frame_offset_db": None, "frame_tilt_db_per_octave": None,
                "frame_n_bins": 0, "frame_band_hz": None,
            },
            (),
            "pass",
            id="delta-probe-level-mismatch",
        ),
        # #2521: the offset and tilt that explain the verdict, and the span
        # they were fitted over — a tilt fitted over a narrow quiet region is
        # free to be large and mean nothing.
        pytest.param(
            {"delta_probe": SimpleNamespace(
                verdict="frame_mismatch", reason="uncommanded_frame_shift",
                expected_offset_db=0.0, residual_offset_db=-2.39,
                frame=SimpleNamespace(
                    offset_db=-2.39, tilt_db_per_octave=-0.916, n_bins=214,
                    band_hz=(120.0, 3_300.0),
                ),
            )},
            "delta_probe",
            {
                **_UNKNOWN_DELTA_PROBE_FIELDS,
                "verdict": "frame_mismatch",
                "reason": "uncommanded_frame_shift",
                "expected_offset_db": 0.0, "residual_offset_db": -2.39,
                "frame_offset_db": -2.39, "frame_tilt_db_per_octave": -0.916,
                "frame_n_bins": 214, "frame_band_hz": [120.0, 3_300.0],
            },
            (),
            "fail",
            id="delta-probe-frame-mismatch",
        ),
        # #1868. ``evidence``'s pass-only suppression is UNCHANGED — only the
        # band, which bounds the claim, is added beside it.
        pytest.param(
            {
                "verify_evidence": {
                    "max_db": 0.9, "rms_db": 0.4, "tolerance_db": 1.5,
                },
                "verify_graded_band_hz": [2000.0, 4000.0],
            },
            "graded_band_hz", [2000.0, 4000.0], ("evidence",), "fail",
            id="graded-band",
        ),
        # R18 (#1868): the band bounds how wide the tracking claim is; the
        # claims say which claims exist at all.
        pytest.param(
            {"verify_claims": _SECTION_7_CLAIMS},
            "claims", _SECTION_7_CLAIMS, (), "fail",
            id="section-7-claims",
        ),
        # Rung P1: a pass is precisely when an undisclosed tilt is dangerous.
        pytest.param(
            {"verify_frame": _COMPARED_FRAME},
            "frame", _COMPARED_FRAME, (), "fail",
            id="compared-frame",
        ),
        # #1966: before R9 this sentence rendered nowhere a screen could read.
        pytest.param(
            {"verify_gate": _GATE_DISCLOSURE},
            "gate", _GATE_DISCLOSURE, (), "fail",
            id="gate-disclosure",
        ),
    ),
)
def test_a_verify_record_is_persisted_even_on_a_pass(
    attrs, key, expected, also_absent, plain_outcome,
):
    """Every VERIFY record the done screen and ``/state`` read has to survive a
    PASSING persist, because a pass is the one outcome nobody interrogates.

    The converse half is the same contract read backwards: a conductor that
    recorded nothing writes no key rather than an empty claim, so absent means
    "this was never measured" and never "measured, and clean".
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "pass"
    for name, value in attrs.items():
        setattr(conductor, name, value)
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(conductor, failure_code=None)

    verify = (v2host.load_v2_state() or {})["verify"]
    assert verify[key] == expected
    for absent in also_absent:
        assert absent not in verify

    plain = _StubConductor("s1")
    plain.verify_outcome = plain_outcome
    v2host.persist_conductor_state(plain, failure_code=None)
    assert key not in (v2host.load_v2_state() or {})["verify"]


def test_the_verify_code_is_persisted_beside_its_outcome():
    """Issue #1974: "inconclusive" is reached by two verdicts with no shared
    mechanism, and the done screen names the cause from the code.

    It is NOT read from ``failure.code``: that is the most recent rejection of
    any phase, and the second persist below — the ordinary shape of a session
    that fails VERIFY and then writes again with nothing failing — nulls the
    failure block while the verify outcome stands. A screen reading it there
    would have lost the cause exactly when it needed it.
    """
    conductor = _StubConductor("s1")
    conductor.verify_outcome = "inconclusive"
    conductor.verify_code = "verify_level_shift"
    v2host.save_v2_state({"session_id": "s1"})
    v2host.persist_conductor_state(
        conductor, failure_code="verify_level_shift",
    )
    state = v2host.load_v2_state() or {}
    assert state["verify"]["code"] == "verify_level_shift"

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}
    assert state["failure"] is None
    assert state["verify"]["code"] == "verify_level_shift"

    # A pass carries no code — nothing rejected it.
    passing = _StubConductor("s1")
    passing.verify_outcome = "pass"
    v2host.persist_conductor_state(passing, failure_code=None)
    assert "code" not in (v2host.load_v2_state() or {})["verify"]


def test_applied_offset_gate_reports_nothing_known_rather_than_guessing():
    """``0.0`` is the honest answer for an absent or malformed value — the
    probe then leaves the whole shift visible in ``residual_offset_db``
    instead of claiming it was accounted for."""
    v2host.save_v2_state({"session_id": "s", "applied": True})
    assert v2host._applied_offset_gate() == 0.0
    for bad in ("loud", None, True, float("nan"), float("inf")):
        # Planted as a file: two of these are values ``save_v2_state`` refuses
        # since #2839, and it is a state FILE this gate has to survive.
        _plant_unbankable_v2_state({
            "session_id": "s", "applied": True,
            "expected_post_apply_offset_db": bad,
        })
        assert v2host._applied_offset_gate() == 0.0


def test_a_pre_pr6b_candidate_payload_still_applies(monkeypatch, tmp_path):
    """Era tolerance at the LIVE surface, not just in ``from_mapping``.

    The blocker this pins: ``to_dict()`` always writes ``exclusion_evidence``,
    so a ``candidate.json`` published by a build that predates the field fails
    ``from_mapping``'s reopen comparison unless it is setdefaulted — and that
    comparison is on the apply path (``handle_v2_apply`` →
    ``_reopen_candidate_artifact`` → ``from_mapping``). The household-visible
    symptom was a ``candidate_tampered`` refusal telling them their persisted
    correction had been altered when the file was merely older than the field.

    Drives the SAME real ``apply_baseline_profile`` path as the sibling test
    above, with the key deleted from the payload — it must load, keep its
    fingerprint, and apply.
    """
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    pre_pr6b_payload = dict(candidate.to_dict())
    assert "exclusion_evidence" in pre_pr6b_payload
    del pre_pr6b_payload["exclusion_evidence"]  # the pre-PR-6b persisted shape

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": pre_pr6b_payload,
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "applied", payload.get("issues")
    assert v2host._applied_gate() is True


def test_apply_refuses_when_composition_is_no_longer_bound_to_reviewed_candidate(
    monkeypatch, tmp_path,
):
    """TOCTOU note pin: the host's own compose-then-verify precheck refuses by
    name (rather than silently applying) if the composition it just built no
    longer binds to the measured candidate the household reviewed — the
    guard the ARCHITECT ruling asked for, exercised directly rather than by
    trying to win a real race."""
    from jasper.active_speaker import baseline_profile as baseline_profile_mod

    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    real_build = baseline_profile_mod.build_baseline_profile_candidate

    def _tampered_build(*args, **kwargs):
        out = dict(real_build(*args, **kwargs))
        source = dict(out.get("source") or {})
        source["measured_candidate_fingerprint"] = "not-the-reviewed-candidate"
        out["source"] = source
        return out

    monkeypatch.setattr(
        baseline_profile_mod, "build_baseline_profile_candidate", _tampered_build,
    )

    with pytest.raises(v2host.CrossoverV2Refused, match="no longer current"):
        _apply(
            {
                "expected_candidate_fingerprint": candidate.fingerprint,
                "candidate": candidate.to_dict(),
            },
            _bg_run_async,
            _FakeApplyCam,
        )
    assert v2host._applied_gate() is False


def test_apply_blocks_and_persists_a_nudge_when_the_reviewed_preset_goes_stale(
    monkeypatch, tmp_path,
):
    """Negative, through the REAL seam: the household reviewed a candidate
    measured against one crossover design, but the design moved on
    underneath (a second /sound/ save, followed by a fresh v2 session start
    that re-ensures the preview) before Apply landed. The seam's own
    ``measured_candidate_preset_mismatch`` gate must refuse — never silently
    apply the wrong preset — and Finding N's wiring must name that issue and
    persist it for the review_apply nudge, instead of 200 + silent no-op."""
    from jasper.active_speaker.design_draft import build_design_draft

    from tests.test_active_speaker_baseline_profile import _research

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    v2host.save_v2_state({
        "session_id": "cap_run6",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })

    # The crossover design moved on (a second tab/session saved a different
    # crossover frequency) after this candidate was measured — write the
    # moved DESIGN DRAFT directly (that part is /sound/'s job, out of this
    # wave's scope), then re-ensure the preview through the REAL seam, the
    # same way a fresh v2 session start would after that save.
    moved_research = _research()
    moved_research["crossover_candidates"][0]["frequency_hz"] = 3000
    moved_draft = build_design_draft(
        topology, driver_research=moved_research, created_at="2026-07-18T12:30:00Z",
    )
    (tmp_path / "design_draft.json").write_text(
        json.dumps(moved_draft), encoding="utf-8"
    )
    v2host.ensure_crossover_preview_ready()

    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )

    assert payload["status"] == "blocked"
    assert payload["issue"]["id"] == "measured_candidate_preset_mismatch"
    # The positive control for the reader the two "this guard did NOT fire"
    # assertions use: a reader that could never see this id would let those
    # tests pass on a payload that names it.
    assert "measured_candidate_preset_mismatch" in _apply_issue_ids(payload)
    assert v2host._applied_gate() is False

    saved_state = v2host.load_v2_state()
    assert saved_state["apply_blocked"] == payload["issue"]


# --- the way-back pointer, through the REAL apply seams -----------------------
#
# ``previous_candidate_fingerprint`` is the way back's only durable pointer:
# it is what a republish-then-apply resolves. These tests drive
# handle_v2_apply through the REAL seams (same fixture shape as the Blocker M
# tests above) — not a faked apply gate.


def _prior_measured_candidate(preset):
    """The household's pre-existing applied crossover — deliberately a
    DIFFERENT measured candidate from the run-8 shape below, so a passing
    revert is proof of reversion rather than a no-op."""
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    return MeasuredCrossoverCandidate(
        program_id="prog-prior-1",
        analysis={"epsilon_ppm": 5.0, "predicted_ripple_db": 1.2,
              "alignment_confidence": 0.82,
              "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4}},
        source_preset=preset,
        role_attenuations_db={"tweeter": -2.0, "woofer": 0.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=250.0, delay_role="tweeter", polarity="keep",
        ),
    )


def test_entry_graph_fingerprint_names_the_applied_profile(monkeypatch):
    """#2291: the entry baseline records WHICH graph it was measured through.

    The conductor's three fallbacks (no seam, seam raised, no applied profile)
    each have coverage; the seam's REAL path — the one production binds on both
    stages — did not, so nothing pinned that it reads the applied SSOT's own
    recomputed ``candidate_fingerprint`` rather than inventing an identity.
    That field is the one `load_applied_baseline_profile_state` re-derives from
    the immutable source, which is precisely why this reads the stored value
    instead of hashing anything itself: one hash function, one definition of
    "which graph".

    The empty answer is pinned beside it because it is not an error. A speaker
    with no applied profile is on its first-ever round, where the entry graph
    genuinely has no identity to name; the conductor turns "" into its own
    ``unknown`` word rather than this function inventing one.
    """
    calls: list[int] = []

    def _applied() -> dict[str, Any]:
        calls.append(1)
        return {"candidate_fingerprint": "fp-live-graph", "status": "applied"}

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        _applied,
    )
    assert v2host._active_graph_fingerprint() == "fp-live-graph"
    assert calls == [1], "the applied SSOT is the only thing consulted"

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda: None,
    )
    assert v2host._active_graph_fingerprint() == ""


def test_the_commanded_axis_seam_refuses_a_displaced_applied_record(
    monkeypatch, caplog,
):
    """#2614: the previous-graph seam applies its sibling's displacement guard.

    A record is only an answer to "which graph is on the speaker" while it is
    still the graph on the speaker. An out-of-band reconcile changes the RUNNING
    config without touching the record — the 2026-08-15 cycle-4 shape — and
    ``_active_graph_fingerprint`` has refused a displaced record since #2537 for
    exactly that reason. This seam makes the same record ROLLBACK-DECIDING, so
    it must refuse it too, and the surface is named on the journal so the two
    refusals are told apart.

    Only a POSITIVE displacement refuses. The other two codes mean the
    comparison could not be made, and an absent measurement is not evidence of a
    defect — a box with no readable statefile would otherwise lose its commanded
    axis forever.
    """
    import logging

    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        APPLIED_PROFILE_RUNNING_UNKNOWN,
    )

    record = {"candidate_fingerprint": "fp-live-graph", "status": "applied"}
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda: record,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda applied, **kwargs: "",
    )
    assert v2host._applied_profile_now() == record

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda applied, **kwargs: APPLIED_PROFILE_RUNNING_UNKNOWN,
    )
    assert v2host._applied_profile_now() == record, (
        "an unreadable statefile is 'we could not check', not 'it moved'"
    )

    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda applied, **kwargs: APPLIED_PROFILE_DISPLACED,
    )
    with caplog.at_level(logging.WARNING):
        assert v2host._applied_profile_now() is None
    assert "event=correction.crossover_v2_applied_profile_displaced" in caplog.text
    assert "surface=commanded_axis" in caplog.text


def test_second_apply_way_back_pointer_survives_the_deferred_verify_rearm(
    monkeypatch, tmp_path,
):
    """W6.12 P0 regression shape: the way-back pointer must survive the
    deferred VERIFY that always auto-arms right after every apply.

    Drives handle_v2_apply TWICE in sequence, both through the production
    seam (not seeded state) — a v2-written prior profile ("run 1"), then a
    v2 apply over it ("run 2 over run 1"), matching the round-4 hardware
    differential. The historical drop was never in
    ``handle_v2_apply``/``observe_apply_success`` (both prove correct here);
    it was that ``persist_conductor_state`` built a fresh state dict that
    never carried the stash forward, so the deferred VERIFY that auto-arms
    after every apply (the verify-only prepare mints a NEW capture session id
    and immediately calls ``persist_conductor_state`` to "rebind" it — see
    its own call site) wiped the just-recorded pointer. This test reproduces
    that exact rebind call (a real ``CrossoverV2Session``, not a mock)
    between each apply and the next, and pins that the pointer survives
    it."""
    from jasper.active_speaker.crossover_v2.journey import PHASE_VERIFY
    from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams

    from tests.crossover_v2_fixtures import CAPS, FC_HZ, SESSION_VOLUME_DB, _roles

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    config_path = tmp_path / "active_speaker_baseline.yml"

    def _simulate_deferred_verify_rearm(*, verify_session_id: str) -> None:
        """Exactly what the verify-only prepare's ``_open`` does: mint a fresh
        conductor bound to a NEW capture session id, applied=True, and
        immediately persist it ("Keep the durable candidate/applied facts;
        rebind the session id.") — the real production seam this regression
        traces to, not a synthetic stand-in."""
        conductor = CrossoverV2Session(
            session_id=verify_session_id,
            source_preset=preset,
            roles_bands=_roles(),
            fc_hz=FC_HZ,
            driver_caps_dbfs=CAPS,
            session_volume_db=SESSION_VOLUME_DB,
            seams=V2FlowSeams(
                play=lambda *a, **k: None,
                analyze=lambda *a, **k: None,
                publish_check=lambda *a, **k: None,
                publish_candidate=lambda *a, **k: None,
                apply_complete=v2host._applied_gate,
                apply_failed=v2host._apply_failure_gate,
            ),
            driver_spacing_m=0.15,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            index_phase_map={1: PHASE_VERIFY},
        )
        v2host.persist_conductor_state(conductor, failure_code=None)

    # --- run 1: a v2-written apply, no pre-existing profile to restore to ---
    run1_candidate = _prior_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run1",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run1_candidate.fingerprint},
        "applied": False,
    })
    run1_payload = _apply(
        {
            "expected_candidate_fingerprint": run1_candidate.fingerprint,
            "candidate": run1_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert run1_payload["status"] == "applied", run1_payload.get("issues")
    # #1666: run 1 (the speaker's first-ever apply) lands on its own
    # source-fingerprinted sibling too, not config_path directly -- read the
    # stable reference value from run 1's own reported path. The successful
    # apply's promote step means config_path (canonical) also currently
    # holds these same bytes, as a COPY.
    run1_config_text = Path(
        run1_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == run1_config_text
    # The speaker's first-ever apply displaced no measured candidate.
    assert v2host.load_v2_state()["previous_candidate_fingerprint"] is None

    # The deferred VERIFY always auto-arms right after an apply — reproduce
    # its rebind-and-persist before the household ever reaches run 2.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run1")
    assert v2host.load_v2_state()["applied"] is True
    assert v2host.load_v2_state()["previous_candidate_fingerprint"] is None

    # --- run 2 over run 1: also v2-written, through the SAME production seam ---
    run2_candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        **v2host.load_v2_state(),
        "session_id": "cap_run2",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run2_candidate.fingerprint},
    })
    run2_payload = _apply(
        {
            "expected_candidate_fingerprint": run2_candidate.fingerprint,
            "candidate": run2_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert run2_payload["status"] == "applied", run2_payload.get("issues")
    # run 1's own sibling file is never clobbered by run 2's apply...
    assert Path(
        run1_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8") == run1_config_text
    # ...but canonical is a promoted COPY of whichever candidate applied most
    # recently (#1666), so it now tracks run 2, not run 1.
    run2_config_text = Path(
        run2_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == run2_config_text
    assert run2_config_text != run1_config_text

    state_after_run2_apply = v2host.load_v2_state()
    assert (
        state_after_run2_apply.get("previous_candidate_fingerprint")
        == run1_candidate.fingerprint
    )

    # The P0 assertion: run 2's own deferred VERIFY rebind must NOT wipe the
    # pointer — this is exactly where the stash went null before the fix.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run2")
    state_after_verify_rearm = v2host.load_v2_state()
    assert state_after_verify_rearm["applied"] is True
    assert (
        state_after_verify_rearm.get("previous_candidate_fingerprint")
        == run1_candidate.fingerprint
    )


def test_start_over_while_applied_keeps_the_way_back_pointers(
    monkeypatch, tmp_path,
):
    """W6.10 gate should-fix: apply the prior crossover, apply a measured
    candidate over it, Start-over (reset_v2_journey_state — what handle_reset
    calls under the v2 flow). The reset must serve the clean start screen
    WITHOUT unlinking `applied` + `previous_candidate_fingerprint` — the way
    back's only durable pointer."""
    from jasper.active_speaker.baseline_profile import apply_baseline_profile
    from jasper.active_speaker.crossover_preview import build_crossover_preview

    from tests.test_active_speaker_baseline_profile import _draft

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-19T09:00:00Z")

    prior_candidate = _prior_measured_candidate(preset)
    prior_cam = _FakeApplyCam()
    prior_payload = _bg_run_async(
        apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=prior_cam.set_config_file_path,
            get_current_config_path=prior_cam.get_config_file_path,
            tuning_owner="automatic",
            measured_candidate=prior_candidate,
        )
    )
    assert prior_payload["status"] == "applied", prior_payload.get("issues")

    run8_candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_run8",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": run8_candidate.fingerprint},
        "applied": False,
    })
    apply_payload = _apply(
        {
            "expected_candidate_fingerprint": run8_candidate.fingerprint,
            "candidate": run8_candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert apply_payload["status"] == "applied", apply_payload.get("issues")

    # Start-over while applied — the selective journey reset.
    v2host.reset_v2_journey_state()

    state = v2host.load_v2_state()
    assert state is not None
    assert state["applied"] is True
    assert state["previous_candidate_fingerprint"] == prior_candidate.fingerprint
    assert state["accepted_phases"] == []
    assert state["candidate"] is None
    # The envelope serves the clean start screen…
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_CHECK


# --- W6.11: the real session-start preview-ensure seam, end to end ---
#
# The P0: ``/sound/``'s Preview button was the ONLY historical writer of
# ``active_speaker_crossover_preview.json``; the v2 flow never called it. A
# candidate measured without a preview baked its ``source_preset`` against
# ``resolve_capture_preset``'s generic-bundled-preset fallback, which then
# could NEVER match a preview generated later — apply refused
# ``measured_candidate_preset_mismatch`` forever, and Start-over (which
# deletes the preview by design, see ``jasper.active_speaker.reset``)
# poisoned every subsequent apply. ``_seed_baseline_apply_environment``
# itself was part of the problem: it hand-built and wrote the preview file
# directly, sidestepping the exact fallback path that shipped broken.
#
# These tests drive the REAL fix end to end, through the real seams, with
# NO hand-seeded preview file anywhere: v2 session start
# (``v2host.ensure_crossover_preview_ready`` — the seam both
# ``resolve_conductor_context`` callers — both stages of
# ``prepare_v2_session`` — share) generates the preview from the current
# design draft when absent, reusing ``/sound/``'s own generator
# (``jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft``
# -> ``crossover_preview.save_crossover_preview``).


def test_v2_session_start_ensures_preview_and_survives_start_over_then_reapply(
    monkeypatch, tmp_path,
):
    """The full real journey: no preview on disk -> session start ensures one
    (asserted on disk, ready) -> measure-shaped candidate baked against the
    resolved preset -> handle_v2_apply SUCCEEDS through the real
    apply_baseline_profile guard -> Start-over (the REAL handle_reset)
    deletes the preview by design -> a fresh session start re-ensures it from
    the (unchanged) design draft -> apply succeeds again. The test never
    once hand-writes active_speaker_crossover_preview.json."""
    from jasper.active_speaker import compile_preset_from_crossover_preview
    from jasper.web import correction_crossover_backend as reset_backend
    from jasper.web import correction_crossover_flow as reset_flow

    preview_path = tmp_path / "crossover_preview.json"
    assert not preview_path.exists()

    # _seed_baseline_apply_environment's own preview-generation step IS a v2
    # session start (it calls ensure_crossover_preview_ready — no direct
    # build_crossover_preview()+write since W6.11). Assert the file landed
    # ready, proving the ensure step actually ran rather than being a no-op.
    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    assert preview_path.exists()
    on_disk = json.loads(preview_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "ready_for_protected_staging"

    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_e2e_1",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload["status"] == "applied", payload.get("issues")

    # Start-over — the REAL handle_reset (real reset_measurement_journey, a
    # fresh no-op CrossoverLevelLease; only the envelope-rendering tail is
    # stubbed, mirroring test_correction_crossover_reset.py's real-clear
    # pattern). The other measurement-journey artifacts route to tmp_path too
    # so the real clear never touches /var/lib/jasper.
    for env_name in (
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE",
    ):
        monkeypatch.setenv(env_name, str(tmp_path / f"{env_name.lower()}.json"))
    fresh_lease = reset_backend.CrossoverLevelLease()
    monkeypatch.setattr(reset_backend, "level_lease", lambda: fresh_lease)
    monkeypatch.setattr(reset_flow, "handle_status", lambda *, capture=None: ({}, 200))
    monkeypatch.setattr(reset_flow, "_active_group_member", lambda: False)
    monkeypatch.setattr(
        "jasper.web.correction_crossover_flow._build_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )

    _reset_payload, reset_status = reset_flow.handle_reset()

    assert reset_status == 200
    # The preview really is gone — reset.py's documented by-design deletion.
    assert not preview_path.exists()

    # A fresh v2 session start re-ensures the preview from the unchanged
    # design draft — still no hand-seeding.
    reensured = v2host.ensure_crossover_preview_ready()
    assert reensured["status"] == "ready_for_protected_staging"
    assert preview_path.exists()

    preset_again, issues, _gates = compile_preset_from_crossover_preview(
        topology, reensured,
    )
    assert preset_again is not None, issues
    candidate_again = _run6_measured_candidate(preset_again)
    v2host.save_v2_state({
        "session_id": "cap_e2e_2",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate_again.fingerprint},
        "applied": False,
    })
    payload_again = _apply(
        {
            "expected_candidate_fingerprint": candidate_again.fingerprint,
            "candidate": candidate_again.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert payload_again["status"] == "applied", payload_again.get("issues")


def test_v2_session_start_refuses_by_name_when_draft_cannot_produce_a_ready_preview(
    monkeypatch, tmp_path,
):
    """Negative: no design draft has ever been saved, so the ensure step's
    regeneration attempt cannot reach ready_for_protected_staging. Session
    start must refuse BY NAME (CrossoverV2Refused, naming the actual
    blocker) — never a silent pass-through that only surfaces as an
    apply-time 409 later."""
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft_never_saved.json"),
    )
    preview_path = tmp_path / "crossover_preview.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE", str(preview_path)
    )

    with pytest.raises(v2host.CrossoverV2Refused, match="not ready for measurement"):
        v2host.ensure_crossover_preview_ready()

    # The regeneration attempt still ran (the same machinery /sound/ would
    # have run) and left an honest "blocked" preview on disk, never a
    # ready_for_protected_staging one.
    assert preview_path.exists()
    blocked = json.loads(preview_path.read_text(encoding="utf-8"))
    assert blocked["status"] == "blocked"


# --------------------------------------------------------------------------- #
# CHECK evidence artifact — the per-role MEASURE level solve (issue #1825)
# --------------------------------------------------------------------------- #


class _RecordingEvidenceStore:
    """Minimal stand-in for the commissioning evidence store."""

    session_id = "bundle-session"

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish_json_artifact(self, relpath, payload):
        self.published.append((relpath, payload))
        return SimpleNamespace(fingerprint="fp-check")


def test_check_evidence_artifact_carries_the_per_role_level_solve():
    """`check.json` is the session's durable record of what MEASURE was about
    to be played at. The solved gains alone are not self-explaining, so the
    artifact carries the derivation beside them — which limit chose each
    driver's level, and the ambient band it was solved against."""
    from jasper.audio_measurement.program_analysis import GainPlan, RoleGainSolve

    store = _RecordingEvidenceStore()
    publish_check, _publish_candidate, refs = v2host.bind_evidence_publishers(
        store, "capture-session"
    )
    plan = GainPlan(
        gain_db={"woofer": -19.0, "tweeter": -31.0},
        predicted_peak_dbfs=-19.0,
        snr_floor_ok=True,
        role_solves={
            "tweeter": RoleGainSolve(
                role="tweeter", gain_db=-31.0, flat_target_gain_db=-13.0,
                bound_by="room_snr", band_hz=(1500.0, 20000.0),
                ambient_dbfs=-72.0, required_snr_db=41.0,
                required_capture_dbfs=-31.0,
            ),
        },
    )
    publish_check(plan, {"bands": [{"band_id": "mid", "level_dbfs": -72.0}]})

    assert refs["check_artifact"] == "fp-check"
    (relpath, raw_payload), = store.published
    assert relpath == "crossover_v2/capture-session/check.json"
    # Round-trips as JSON — the evidence store re-opens what it writes.
    payload = json.loads(json.dumps(raw_payload))
    assert payload["gain_plan_db"] == {"woofer": -19.0, "tweeter": -31.0}
    tweeter = payload["role_solves"]["tweeter"]
    assert tweeter["bound_by"] == "room_snr"
    assert tweeter["reduction_db"] == pytest.approx(18.0)
    assert tweeter["ambient_dbfs"] == pytest.approx(-72.0)
    assert tweeter["band_hz"] == [1500.0, 20000.0]


def test_check_evidence_artifact_tolerates_a_plan_without_solves():
    """A legacy plan carries no ``role_solves``; the artifact publishes an
    empty map rather than failing — and an empty map is "no derivation
    published", never a claim that nothing moved."""
    from jasper.audio_measurement.program_analysis import GainPlan

    store = _RecordingEvidenceStore()
    publish_check, _publish_candidate, _refs = v2host.bind_evidence_publishers(
        store, "capture-session"
    )
    publish_check(
        GainPlan(
            gain_db={"woofer": -11.0}, predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
        {"bands": []},
    )
    (_relpath, payload), = store.published
    assert payload["role_solves"] == {}


# --- #2519: Undo has to survive a verify re-arm, through the REAL seams -------
#
# The night this was filed, a jts3 apply was followed by a ``verify_retry``
# re-arm (issue #2517 forced one), and from then on BOTH the delta probe's
# automatic rollback and the household's own Undo refused deterministically —
# nine minutes apart, with the same message about a candidate that had
# "changed after validation and before load" over a config file nothing had
# touched. Re-arms are ordinary, so the anchor surviving one is not an
# incidental property to leave to argument: these drive apply → re-arm → Undo
# end to end, over the real apply/restore transaction.


def _bank_candidate(monkeypatch, tmp_path, candidate) -> None:
    """Publish ``candidate`` into a bundle bank, at the path its minting capture
    session would have used — the artifact the automatic way back republishes."""
    root = tmp_path / "bank-sessions"
    path = (
        root / "bundleprior00" / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / "capture-prior-1" / "candidate.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: root)


def _apply_prior_then_v2_candidate(monkeypatch, tmp_path):
    """Apply the household's pre-existing crossover, then a v2 measured
    candidate over it — the state a household is in when VERIFY arms. Returns
    the durable v2 state's Undo anchor. The prior candidate is also banked, as
    its own measure session would have left it, so the automatic way back can
    republish it."""
    from jasper.active_speaker.baseline_profile import apply_baseline_profile
    from jasper.active_speaker.crossover_preview import build_crossover_preview

    from tests.test_active_speaker_baseline_profile import _draft

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")

    prior_candidate = _prior_measured_candidate(preset)
    _bank_candidate(monkeypatch, tmp_path, prior_candidate)
    prior_cam = _FakeApplyCam()
    prior_payload = _bg_run_async(
        apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=prior_cam.set_config_file_path,
            get_current_config_path=prior_cam.get_config_file_path,
            tuning_owner="automatic",
            measured_candidate=prior_candidate,
        )
    )
    assert prior_payload["status"] == "applied", prior_payload.get("issues")

    candidate = _run6_measured_candidate(preset)
    v2host.save_v2_state({
        "session_id": "cap_apply",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": candidate.fingerprint},
        "applied": False,
    })
    apply_payload = _apply(
        {
            "expected_candidate_fingerprint": candidate.fingerprint,
            "candidate": candidate.to_dict(),
        },
        _bg_run_async,
        _FakeApplyCam,
    )
    assert apply_payload["status"] == "applied", apply_payload.get("issues")

    pointer = (v2host.load_v2_state() or {}).get("previous_candidate_fingerprint")
    assert pointer, "the apply must record the displaced measured candidate"
    return pointer


def test_the_rearm_stand_in_matches_prepare_v2_sessions_durable_write_set():
    """``_rearm_verify`` below is a stand-in, and a stand-in is only evidence
    while it stays faithful. Its fidelity claim is narrow and checkable:
    the verify-only prepare reaches durable v2 state through
    ``persist_conductor_state`` and nothing else. A future re-arm that writes
    the state file by another route fails here rather than quietly making the
    two tests below prove something about a shape production no longer has."""
    import inspect

    source = inspect.getsource(v2host.prepare_v2_session)

    assert "persist_conductor_state(" in source
    for direct_writer in (
        "save_v2_state(", "clear_v2_state(", "reset_v2_journey_state(",
        "observe_apply_success(", "_update_current_review(",
    ):
        assert direct_writer not in source, direct_writer


def _rearm_verify():
    """What the verify-only prepare does to durable state on a ``verify_retry``:
    re-derive the session context (which re-ensures the crossover preview) and
    persist a conductor under a BRAND-NEW session id. The write-set fidelity
    of this stand-in is pinned by the test directly above."""
    v2host.ensure_crossover_preview_ready()
    v2host.persist_conductor_state(_StubConductor("cap_rearm"), failure_code=None)


def test_the_delta_probe_rollback_still_restores_after_a_verify_rearm(
    monkeypatch, tmp_path,
):
    """The automatic half of the same guarantee, through the NORMAL path.

    The rollback resolves the recorded ``previous_candidate_fingerprint``
    — which must survive the re-arm — republishes that banked candidate, and
    drives the REAL apply transaction to completion. Success leaves the prior
    candidate APPLIED (not an un-applied speaker: the way back is itself an
    apply), with the way-back pointer re-armed at the graph this revert
    displaced. The proof expectation reaching the DSP transaction is the
    recomposed candidate's own digest — real, never the empty expectation that
    refuses unconditionally."""
    prior_fingerprint = _apply_prior_then_v2_candidate(monkeypatch, tmp_path)

    _rearm_verify()

    seen: dict[str, object] = {}
    real_apply = baseline_profile_mod.apply_dsp_config

    async def observed_apply(**kwargs):
        seen["expected"] = kwargs.get("expected_candidate_sha256")
        return await real_apply(**kwargs)

    monkeypatch.setattr(baseline_profile_mod, "apply_dsp_config", observed_apply)
    monkeypatch.setattr(
        correction_crossover_backend, "status_payload", lambda: {},
    )

    rollback = v2host.bind_delta_probe_rollback(_bg_run_async, _FakeApplyCam)

    with _stage2_openable():
        assert rollback("realized_shape_differs_from_commanded") is True
    # Non-empty is the load-bearing half: an empty expectation is a guaranteed
    # proof refusal, which is what a lost way back would look like from inside
    # the transaction.
    assert seen["expected"]
    state = v2host.load_v2_state() or {}
    assert state["applied"] is True
    assert state["candidate"]["fingerprint"] == prior_fingerprint
    # The revert re-stamped the way back at the candidate it displaced, so a
    # household can come forward again through the same door…
    assert state["previous_candidate_fingerprint"] not in ("", None)
    assert state["previous_candidate_fingerprint"] != prior_fingerprint
    # …but with its pairing CONSUMED, so the automatic path cannot follow
    # that pointer back inside the [revert…next-apply] window. The button
    # is the household's; the auto path waits for the next ordinary apply.
    assert state["previous_candidate_displaced_by"] is None


# --- #2519: a refused restore has to SAY why, in the only record it has ------


@pytest.mark.parametrize("result", [
    "anchor_missing", "candidate_unreadable", "candidate_changed",
])
def test_every_proof_refusal_classifies_as_known_inactive(result):
    """The "imported, not transcribed" claim, with teeth. Rebinding
    ``DSP_PROOF_INACTIVE_RESULTS`` to the pre-#2519 transcription
    (``{"candidate_changed"}``) passed the whole suite — so the silent
    degradation this PR exists to prevent had no guard at all.

    What degrading costs: a proof refusal never reaches ``load_config``, so the
    speaker is provably untouched. Reading one as UNKNOWN routes the household
    to ``apply_result_unknown`` — "JTS could not confirm whether DSP apply
    finished; review the current speaker state" — about an apply that did
    nothing."""
    payload = {
        "status": "apply_failed",
        "apply": {"phase": "proof", "result": result, "finished_at": "now"},
    }

    assert v2host._dsp_apply_is_known_inactive(payload) is True


def test_an_unrecognised_proof_result_is_not_assumed_inactive():
    """The other direction, so the test above is a claim about the SET rather
    than about the phase. A proof outcome nobody has classified must read as
    unknown — fail closed, exactly as an unhandled load-phase result does."""
    payload = {
        "status": "apply_failed",
        "apply": {"phase": "proof", "result": "some_future_result",
                  "finished_at": "now"},
    }

    assert v2host._dsp_apply_is_known_inactive(payload) is False


def test_a_corrupted_bank_refuses_the_automatic_way_back_loudly(
    monkeypatch, tmp_path, caplog,
):
    """The delta probe's rollback reduces the doors' outcome to a bool for its
    conductor and has no household screen, so the journal is the ONLY place
    its refusal reason can exist.

    A single flipped byte in the banked artifact must refuse the republish
    (the candidate model's own recompute-and-compare), reach the seam as
    "not restored", and leave the regressed graph's record untouched — a
    revert that could not verify its target must not move anything.
    """
    _apply_prior_then_v2_candidate(monkeypatch, tmp_path)
    from jasper.active_speaker.bundles import sessions_dir

    artifact = next(sessions_dir().glob("*/evidence/v1/artifacts/crossover_v2/*/candidate.json"))
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("prog-prior-1", "prog-tampered"),
        encoding="utf-8",
    )
    rollback = v2host.bind_delta_probe_rollback(_bg_run_async, _FakeApplyCam)

    with caplog.at_level(logging.INFO, logger="jasper.web.correction_crossover_v2"):
        assert rollback("realized_shape_differs_from_commanded") is False

    refused_lines = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith(
            "event=correction.crossover_v2_delta_probe_restore_refused "
        )
    ]
    assert len(refused_lines) == 1, refused_lines
    # The fingerprint it aimed at rides the line, so a support read can tell
    # WHICH candidate could not come back.
    assert "candidate_fingerprint=" in refused_lines[0]
    assert (v2host.load_v2_state() or {})["applied"] is True


def test_a_persist_after_a_rollback_keeps_the_reverted_candidate_applied(
    monkeypatch, tmp_path,
):
    """#2616's successor: the live session must not falsify what a revert did.

    The production shape exactly: a stage-2 conductor is live in memory when
    the round's rollback seam republishes-and-applies the prior candidate —
    holding no conductor — and then the ordinary post-capture
    ``persist_conductor_state`` runs. The revert leaves the speaker APPLIED
    (the way back is itself an apply), so the persist must keep saying so, and
    must carry the REVERTED candidate's identity forward rather than erasing
    the slot the republish just restored — the slot the household's next apply
    or review reads.
    """
    prior_fingerprint = _apply_prior_then_v2_candidate(monkeypatch, tmp_path)

    conductor = CrossoverV2Session(
        session_id="cap_2616",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda *a, **k: None,
            analyze=lambda *a, **k: None,
            publish_check=lambda *a, **k: None,
            publish_candidate=lambda *a, **k: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map={1: PHASE_VERIFY},
    )
    v2host.persist_conductor_state(conductor, failure_code=None)
    assert (v2host.load_v2_state() or {})["applied"] is True
    assert conductor.applied is True

    monkeypatch.setattr(
        correction_crossover_backend, "status_payload", lambda: {},
    )
    rollback = v2host.bind_delta_probe_rollback(_bg_run_async, _FakeApplyCam)
    with _stage2_openable():
        assert rollback("realized_shape_differs_from_commanded") is True
    state = v2host.load_v2_state() or {}
    assert state["applied"] is True
    assert state["candidate"]["fingerprint"] == prior_fingerprint

    # An ordinary persist, from the same live session.
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state() or {}
    assert state["applied"] is True
    assert state["candidate"]["fingerprint"] == prior_fingerprint
    assert conductor.applied is True


def test_a_graded_round_reverts_through_the_real_doors_and_the_window_stays_shut(
    monkeypatch, tmp_path,
):
    """The whole-state replacement BETWEEN the doors, pinned inside a round.

    Every round suite stubs the two doors (their own gates have their own
    suites), so the one thing nothing pinned was the door-to-door contract a
    LIVE adoption restore rides: republish replaces the durable session
    document wholesale, and the apply door must still admit the republished
    prior. This drives ``coordinator.run_round`` — the real adoption act —
    over the REAL seam, REAL republish (real bank), and the REAL apply
    transaction, then closes the loop on the review's ping-pong window:

    * the restore round ends ``REFUSAL_RESTORED`` with the prior candidate
      APPLIED (the way back is itself an apply);
    * the pointer is re-stamped at the displaced candidate with its pairing
      CONSUMED by the revert;
    * a second graded round in the [revert…next-apply] window — same
      restore-worthy evidence, fresh binding, so the once-guard is not what
      saves it — routes to ``RECOVERY_REQUIRED`` with nothing attempted, and
      the measured-worse graph stays off the speaker.
    """
    from jasper.active_speaker.crossover_v2 import coordinator as round_coordinator

    prior_fingerprint = _apply_prior_then_v2_candidate(monkeypatch, tmp_path)
    regressed_fingerprint = (v2host.load_v2_state() or {})["candidate"]["fingerprint"]
    monkeypatch.setattr(
        correction_crossover_backend, "status_payload", lambda: {},
    )

    def _graded_round(session_id: str) -> Any:
        # No usable post-apply analysis + a boosted applied graph is the
        # adoption table's fail-closed restore row (row4_untrusted_evidence)
        # — the smallest evidence that makes a REAL round decide RESTORE.
        evidence = round_coordinator.RoundEvidence(
            session_id=session_id,
            tier="express",
            post_analysis=None,
            entry_baseline=None,
            spec_report=None,
            proposal_fingerprint="fp-proposal",
            commanded_delta_present=False,
            realization_tolerance_db=1.0,
            reference_mark="design_axis",
            proposal_fingerprint_kind="candidate",
            candidate_fingerprint=(
                (v2host.load_v2_state() or {}).get("candidate") or {}
            ).get("fingerprint", ""),
            delta_probe=None,
            round_ordinal=1,
            previous_objectives=None,
        )
        ports = round_coordinator.RoundPorts(
            # A FRESH binding per round: the once-guard memo must not be what
            # keeps the window shut.
            rollback=v2host.bind_delta_probe_rollback(_bg_run_async, _FakeApplyCam),
            rollback_available=v2host._previous_candidate_known,
            applied_boosts=lambda: True,
        )
        with _stage2_openable():
            return round_coordinator.run_round(evidence, ports)

    first = _graded_round("cap_revert_round")

    assert first.refusal is not None
    assert first.refusal.kind == round_coordinator.REFUSAL_RESTORED
    state = v2host.load_v2_state() or {}
    assert state["applied"] is True
    assert state["candidate"]["fingerprint"] == prior_fingerprint
    assert state["previous_candidate_fingerprint"] == regressed_fingerprint
    assert state["previous_candidate_displaced_by"] is None

    second = _graded_round("cap_window_round")

    assert second.refusal is not None
    assert second.refusal.kind == round_coordinator.REFUSAL_ROLLBACK_FAILED
    assert second.refusal.rollback_anchor_available is False
    after = v2host.load_v2_state() or {}
    # Nothing moved: the prior candidate is still the applied one, and the
    # measured-worse graph was not automatically re-applied.
    assert after["applied"] is True
    assert after["candidate"]["fingerprint"] == prior_fingerprint


def test_the_status_block_withholds_a_way_back_its_door_would_refuse(
    monkeypatch, tmp_path,
):
    """Review row 6, at the seam that feeds every screen.

    ``previous_candidate_fingerprint`` on the status block is what the
    envelope mints the way-back button AND selects the rollback-failed copy
    arm from. When the republish door would refuse the pointer — here a bank
    with no verifiable artifact for it — the block publishes ``None``, so no
    surface advertises a "Go back to the previous tuning" that refuses on the
    same fact. The positive control drives the REAL preflight over a REAL
    bank: the same state publishes the fingerprint once the artifact is
    admissible.
    """
    prior_fingerprint = _apply_prior_then_v2_candidate(monkeypatch, tmp_path)

    assert (
        v2status.crossover_v2_status_block()["previous_candidate_fingerprint"]
        == prior_fingerprint
    )

    # Prune the bank out from under the pointer: the answer must flip with it.
    monkeypatch.setattr(
        "jasper.active_speaker.bundles.sessions_dir",
        lambda: tmp_path / "empty-bank",
    )
    assert (
        v2status.crossover_v2_status_block()["previous_candidate_fingerprint"]
        is None
    )


# --- the request-time topology pin: ONE corner + order, for ONE round --------
#
# ``jasper/active_speaker/crossover_v2/topology_prescription.py`` owns the gate
# and every bound it applies; that module's own suite owns those. What is
# pinned HERE is the DOOR the gate is bolted to — the request boundary
# ``prepare_v2_session`` is, and the durable read-back its verify-only
# stage re-opens from:
#
#   1. the ORDER the boundary reads its two request-body prescriptions in,
#      because the delay gate's bound is a half-period AT the corner the
#      topology gate has just moved;
#   2. that a pin is per-round and NEVER inherited, unlike the tier beside it;
#   3. that the accepted record survives the persist → read-back round trip
#      losslessly, which the grading stage re-opens its session from;
#   4. that an inadmissible pin refuses AT THE TAP, before any side effect.


class _StoppedAtTheTap(Exception):
    """Cut the preparer off INSIDE the gate under test.

    Raised from a patched prescription gate so a test never runs a line past
    the fact it is about: no evidence bundle, no capture-source probe, no
    durable write. Deliberately not ``pytest.fail`` (a ``BaseException``, which
    a ``pytest.raises`` cannot usefully name) and deliberately not
    ``contextlib.suppress(Exception)`` — a bare suppress would swallow a
    genuine refusal raised BEFORE the tap and let a recorder assertion pass on
    a preparer that never reached the gate at all.
    """


#: The pinned candidate. Legal for the fixture speaker's declarations below and
#: nowhere near ``FC_HZ`` (1600.0, the corner it is commissioned at), so
#: "the pin took effect" and "the incumbent answered" can never tie.
_PIN_FC_HZ = 2400.0

#: Order 4 is 24 dB/octave, which exactly MEETS the tweeter's declared
#: protective minimum below. Exactness is legal in this repository's gates, so
#: a candidate on the edge is the honest default for a fixture.
_PIN_ORDER = 4

#: The fixture speaker's own declarations, quoted once. ``_roles()`` supplies
#: the two role bands (woofer 150-6000 Hz, tweeter 300-20000 Hz), so the gate's
#: declared floor is 300.0 and its lower-driver ceiling is 6000.0; these two are
#: the declarations ``_roles()`` does not carry. Those role bands are the WHOLE
#: frequency gate since #2870 deleted the crossover search band, which is what
#: makes 2400.0 admissible and 6500.0 — past the woofer's own declared ceiling —
#: refusable.
#: What the fixture tweeter's MAKER publishes, and the only slope the gate may
#: refuse on since #2897. It is deliberately different from the 24.0 stamped on
#: the protective high-pass below — that number is
#: ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)``, a code figure, and
#: a fixture where the two agreed could not tell which one reached the gate.
_PUBLISHED_TWEETER_HP_SLOPE_DB_PER_OCTAVE = 18.0
#: What this build DERIVES and emits as the protective high-pass. Read by the
#: commissioning admission path against a graph this build wrote; never by the
#: topology gate.
_DERIVED_TWEETER_HP_SLOPE_DB_PER_OCTAVE = 24.0
_DECLARED_WOOFER_DIAMETER_MM = 114.0

_PIN_SAFETY_PROFILE = {
    "targets": [
        {
            "role": "woofer",
            "target_fingerprint": "fp-woofer",
            "required_protection_filters": [{
                "kind": "lowpass",
                "cutoff_hz": 6000.0,
                "minimum_slope_db_per_octave": 24.0,
            }],
        },
        {
            "role": "tweeter",
            "target_fingerprint": "fp-tweeter",
            "recommended_highpass_hz": 300.0,
            "recommended_highpass_slope_db_per_octave":
                _PUBLISHED_TWEETER_HP_SLOPE_DB_PER_OCTAVE,
            "required_protection_filters": [{
                "kind": "highpass",
                "cutoff_hz": 300.0,
                "minimum_slope_db_per_octave":
                    _DERIVED_TWEETER_HP_SLOPE_DB_PER_OCTAVE,
            }],
        },
    ],
}


def _topology_pin(**overrides: Any) -> dict[str, Any]:
    """One well-formed ``topology_prescription`` request block.

    Overrides change ONE field, so a test that means "this corner is
    inadmissible" cannot accidentally also be testing a missing provenance.
    """
    body: dict[str, Any] = {
        "kind": "jts_crossover_topology_prescription",
        "artifact_schema_version": 1,
        "fc_hz": _PIN_FC_HZ,
        "order": _PIN_ORDER,
        # The `arm-2.json` filename is banked history, not prose: it is a real
        # path under a real receipts tree and is left exactly as recorded.
        # Invariant 9's rename is forward-only for identifiers and paths.
        "basis_artifacts": ["captures/offline-fc-search/arm-2.json"],
        "basis_note": "candidate 2 of a pre-registered Fc/slope tournament",
    }
    body.update(overrides)
    return body


def _pinnable_context() -> SimpleNamespace:
    """A conductor context whose DECLARATIONS admit ``_topology_pin()``.

    Stubbed rather than resolved from a live topology for the reason
    ``test_prepare_refuses_unrepresentable_confirmed_protection_before_bundle``
    above stubs the same seam: the resolver's own wiring has its own suite
    (``tests/test_correction_crossover_v2_conductor_context.py``), and what
    these tests are about is what the preparer DOES with a context, not how it
    obtains one. Every field below is one the preparer reads before its two
    prescription gates answer.
    """
    return SimpleNamespace(
        preset=_preset(),
        # The corner this speaker is commissioned at — the answer a pinned
        # round must NOT get, and the answer an unpinned one must.
        fc_hz=FC_HZ,
        roles_bands=tuple(_roles()),
        radiating_diameter_mm_by_role={"woofer": _DECLARED_WOOFER_DIAMETER_MM},
        safety_profile=_PIN_SAFETY_PROFILE,
        role_targets={"woofer": "fp-woofer", "tweeter": "fp-tweeter"},
        driver_caps_dbfs=dict(CAPS),
        session_volume_db=SESSION_VOLUME_DB,
        topology=SimpleNamespace(topology_id="t-pin"),
    )


def _arm_stage_1(monkeypatch) -> None:
    """Everything ``prepare_v2_session`` needs BEFORE its prescription gates.

    The evidence store is armed to fail rather than stubbed: every test here
    asserts about a decision the preparer takes before any bundle is opened, so
    a bundle opening at all is the failure, not a fixture gap.
    """
    v2host.set_volume_plan_for_tests(SimpleNamespace(needs_recovery=False))
    monkeypatch.setattr(
        v2host, "reconcile_session_volume_for_new_session", lambda *_a: None,
    )
    monkeypatch.setattr(
        v2host, "resolve_conductor_context", lambda _status: _pinnable_context(),
    )
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda *_a: pytest.fail(
            "the evidence bundle opened before the prescription gates answered"
        ),
    )


def _stage_1_prescription_taps(monkeypatch, body: Any) -> dict[str, Any]:
    """Drive the REAL preparer and report what each prescription gate was asked.

    The topology gate runs FOR REAL (recorded, not replaced), so a refusal is
    still the production refusal. The delay gate is the stopping point: it
    records its arguments and raises :class:`_StoppedAtTheTap`, which is what
    makes the recorded corner evidence about the ORDER of the two reads rather
    than about whatever a later stage happened to do with it.

    Both gates are patched on the modules that OWN them, never on the web
    module: ``prepare_v2_session`` imports each name inside its own body, so a
    name patched on the importer is a name the preparer never looks at.
    """
    from jasper.active_speaker.crossover_v2 import (
        alignment_prescription as alignment_mod,
    )
    from jasper.active_speaker.crossover_v2 import capture_plan as plan_mod
    from jasper.active_speaker.crossover_v2 import (
        topology_prescription as topology_mod,
    )

    _arm_stage_1(monkeypatch)
    seen: dict[str, Any] = {}
    real_shape = plan_mod.resolve_plan_shape
    real_topology_gate = topology_mod.read_topology_prescription

    def _shape(tier=None, **kwargs):
        seen["tier"] = tier
        return real_shape(tier, **kwargs)

    def _topology(raw, **kwargs):
        seen["topology_raw"] = raw
        seen["topology"] = real_topology_gate(raw, **kwargs)
        return seen["topology"]

    def _alignment(raw, *, fc_hz, declared_bounds_us, way_count=None):
        seen["alignment_raw"] = raw
        seen["alignment_fc_hz"] = fc_hz
        seen["alignment_bounds_us"] = declared_bounds_us
        seen["alignment_way_count"] = way_count
        raise _StoppedAtTheTap("the delay gate was reached")

    monkeypatch.setattr(plan_mod, "resolve_plan_shape", _shape)
    monkeypatch.setattr(topology_mod, "read_topology_prescription", _topology)
    monkeypatch.setattr(alignment_mod, "read_alignment_prescription", _alignment)

    with pytest.raises(_StoppedAtTheTap):
        v2host.prepare_v2_session(
            body, status={}, run_async=None, camilla_factory=None,
        )
    return seen


def test_a_pinned_round_bounds_its_delay_at_the_pinned_corner_not_the_incumbent(
    monkeypatch,
):
    """The ordering pin, and it is the load-bearing one in this group.

    ``read_alignment_prescription``'s bound is a HALF-PERIOD of the crossover
    corner: half a cycle at 1600 Hz is 312.5 us and half a cycle at 2400 Hz is
    208.3 us, so the two corners do not merely disagree about a label — they
    admit different delays. A boundary that read the delay prescription first,
    or that kept handing it ``context.fc_hz`` after the topology pin moved the
    round, would gate a 2400 Hz candidate against the 1600 Hz lobe: a gate
    that passes commitments the round it is gating cannot support, with the
    candidate's own name on the receipt.

    So the corner the delay gate is HANDED is asserted, at the gate, rather
    than a later symptom of it. Both prescriptions ride the same request,
    because "both were sent" is the premise the ordering question only exists
    under — a body carrying one of them could not tell a correct order from an
    accidental one.
    """
    seen = _stage_1_prescription_taps(monkeypatch, {
        "topology_prescription": _topology_pin(),
        "alignment_prescription": {"delay_us": 120.0, "basis": "offline"},
    })

    # The premise: both request blocks really reached their own gate.
    assert seen["topology_raw"] == _topology_pin()
    assert seen["alignment_raw"] == {"delay_us": 120.0, "basis": "offline"}
    # The topology gate accepted the pin, so this round's corner IS 2400 Hz...
    assert seen["topology"] is not None
    assert seen["topology"].fc_hz == _PIN_FC_HZ
    # ...and that is the corner the delay bound was derived from.
    assert seen["alignment_fc_hz"] == _PIN_FC_HZ
    # Stated as its own assertion rather than left implied by the line above:
    # the incumbent corner is a real, reachable, DIFFERENT number, which is
    # what makes the equality above a decision instead of a coincidence.
    assert FC_HZ != _PIN_FC_HZ
    assert seen["alignment_fc_hz"] != FC_HZ


def test_a_topology_pin_is_never_inherited_the_way_the_tier_deliberately_is(
    monkeypatch,
):
    """A pin is one round's explicit instruction; the tier is an instrument.

    ``prepare_v2_session`` reads the LAPSED session's tier out of durable state
    when the body names none (#2639 — every "measure again" action the envelope
    mints posts ``{}``, and a REMOTE session's own retry was silently minting
    ``full``; pinned by
    ``tests/test_crossover_v2_remote_tier.py::test_a_re_measure_with_no_tier_inherits_the_lapsed_sessions``).
    A prescription must NOT travel that road. "Measure again" would then re-run
    a tournament candidate nobody asked for, at a corner the speaker is not
    commissioned for, and bank a receipt carrying that candidate's name — the
    same class of dishonesty as clamping an inadmissible pin to a legal one.

    Both facts are read out of ONE durable state in one run, so the test cannot
    pass by the state being unreadable: the tier IS inherited from it, the
    banked pin is proven rehydratable from it, and the gate is still never
    reached.

    "Never reached" rather than "handed ``None``" because the boundary
    short-circuits on the request key's absence — it derives the declarations
    only for a request that will be judged against them, so an unpinned round
    does not consult the topology gate at all. That is the stronger form of the
    same claim: no pin was read from anywhere.
    """
    v2host.save_v2_state({
        "session_id": "cap_lapsed_pinned_round",
        "tier": TIER_EXPRESS,
        "verify_priors": {"topology_prescription": _topology_pin()},
    })
    state = v2host.load_v2_state()
    # The durable pin is READABLE — so "the gate saw nothing" below is a
    # decision the boundary took, never a record it failed to parse.
    banked = v2host.topology_prescription_prior_from_state(state)
    assert banked is not None and banked.fc_hz == _PIN_FC_HZ

    seen = _stage_1_prescription_taps(monkeypatch, {})

    # Not inherited: the topology gate is never consulted at all, so there is
    # no route by which the banked candidate could have reached this round.
    assert "topology_raw" not in seen
    assert "topology" not in seen
    # ...so the round really is unpinned end to end — the delay bound comes off
    # the speaker's commissioned corner, not off the banked candidate's.
    assert seen["alignment_fc_hz"] == FC_HZ
    # The contrast, from the same state file in the same run: the tier IS
    # inherited. Without this the test would also pass on a preparer that had
    # simply stopped reading durable state at all.
    assert seen["tier"] == TIER_EXPRESS


def test_an_order_2_pin_is_admitted_when_the_maker_published_no_slope(
    monkeypatch,
) -> None:
    """The 2026-08-23 owner ruling, end to end through the real preparer.

    The confirmed target still carries a 24 dB/octave protective high-pass —
    that is what this build EMITS — but its maker published no slope condition,
    so there is nothing for the gate to refuse. Before #2897 the derived 24
    reached the gate wearing the manufacturer's clothes and this pin came back
    ``topology_slope_below_declared_requirement``.
    """
    unpublished = deepcopy(_PIN_SAFETY_PROFILE)
    del unpublished["targets"][1]["recommended_highpass_slope_db_per_octave"]
    # Patched on this module's global rather than on the context: ``_arm_stage_1``
    # builds a fresh ``_pinnable_context()`` inside the tap helper, so a context
    # edited out here would be thrown away before the gate ran.
    monkeypatch.setattr(
        sys.modules[__name__], "_PIN_SAFETY_PROFILE", unpublished,
    )

    accepted = _stage_1_prescription_taps(
        monkeypatch, {"topology_prescription": _topology_pin(order=2)},
    )["topology"]

    assert accepted is not None
    assert accepted.order == 2
    assert accepted.slope_db_per_octave == 12.0
    # No published condition, so no comparison was made…
    assert accepted.checked_against_slope_db_per_octave is None
    # …and the recommendation this round crossed under is on the record.
    assert accepted.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )


def test_a_pre_field_profile_refuses_no_slope_and_its_receipt_says_only_that(
    monkeypatch,
) -> None:
    """The LEGACY cause of an empty slope slot, kept distinct from the other one.

    The test above is a maker who prints no qualifier. This one is every speaker
    already commissioned when #2897 deploys: the confirmed target predates the
    owner pair, so it carries NEITHER field — and the consequence is stronger
    than "this driver publishes nothing". No driver on such a profile has a
    published slope, including one whose datasheet says 24, until the next
    ``/sound/`` save re-derives the target. Both causes land the same behaviour
    (no slope refusal, empty receipt slot), and the receipt cannot tell them
    apart — which is exactly why the docstrings must not read the empty slot as
    a datasheet fact.
    """
    pre_field = deepcopy(_PIN_SAFETY_PROFILE)
    tweeter = pre_field["targets"][1]
    del tweeter["recommended_highpass_hz"]
    del tweeter["recommended_highpass_slope_db_per_octave"]
    # The derived protective high-pass is untouched: a pre-field target carries
    # the projections and nothing else, which is what makes this the real shape
    # rather than a profile with a field surgically removed.
    assert tweeter["required_protection_filters"][0]["minimum_slope_db_per_octave"] == (
        _DERIVED_TWEETER_HP_SLOPE_DB_PER_OCTAVE
    )
    monkeypatch.setattr(sys.modules[__name__], "_PIN_SAFETY_PROFILE", pre_field)

    accepted = _stage_1_prescription_taps(
        monkeypatch, {"topology_prescription": _topology_pin(order=2)},
    )["topology"]

    assert accepted is not None
    assert accepted.order == 2
    # Admitted — and the 18 the maker really publishes did NOT reach the gate,
    # because the field that carries it is not on this profile.
    assert accepted.checked_against_slope_db_per_octave is None
    assert accepted.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )
    # The same pin against the SAME maker, once a /sound/ save has put its
    # published 18 on the record, IS refused — so the empty slot above is the
    # profile's age and not the datasheet's silence. Undoing the patch restores
    # the module fixture, which is that saved shape.
    monkeypatch.undo()
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _stage_1_prescription_taps(
            monkeypatch, {"topology_prescription": _topology_pin(order=2)},
        )
    assert "published minimum of 18 dB/octave" in str(excinfo.value)


def test_a_pinned_rounds_record_survives_the_persist_and_rehydrates_equal(
    monkeypatch,
):
    """Stage 2 re-opens AT this record, so a lossy round trip is not cosmetic.

    The verify-only prepare reads the pin back out of ``verify_priors`` and
    re-points its own preset and corner from it. A record that failed to
    rehydrate would grade a 2400 Hz round's VERIFY against the incumbent
    corner's design target — the applied graph judged for not being the
    crossover it deliberately replaced.

    The round trip is asserted LOSSLESS (``to_dict()`` on both sides) rather
    than merely present, because
    ``topology_prescription_from_mapping`` REFUSES an unknown field instead of
    ignoring it: one extra key anywhere in the banked block turns the whole
    record into ``None``, and a "is not None" assertion would not see a gate
    stamp quietly dropped on the way through. The record is taken from the REAL
    request gate rather than hand-built, so the ``checked_against_*`` stamps and
    the beaming disclosure the gate writes are all in the block being
    round-tripped.
    """
    accepted = _stage_1_prescription_taps(
        monkeypatch, {"topology_prescription": _topology_pin()},
    )["topology"]
    # The gate's own stamps are present, which is what makes the round trip
    # below a demanding one rather than four fields wide.
    assert accepted.checked_against_floor_hz == 300.0
    assert accepted.checked_against_ceiling_hz == 6000.0
    # The PUBLISHED condition reached the gate end to end, and the derived 24.0
    # sitting beside it on the same profile target did not (#2897).
    assert accepted.checked_against_slope_db_per_octave == (
        _PUBLISHED_TWEETER_HP_SLOPE_DB_PER_OCTAVE
    )
    assert accepted.checked_against_slope_db_per_octave != (
        _DERIVED_TWEETER_HP_SLOPE_DB_PER_OCTAVE
    )
    # The ka onset is DISCLOSED as a number, never enforced (#1675): this
    # candidate is above it and the receipt says so rather than refusing.
    assert accepted.beaming_ceiling_hz is not None
    assert accepted.fc_hz > accepted.beaming_ceiling_hz
    # …and so is the commissioning slope recommendation, on the same terms.
    assert accepted.recommended_slope_db_per_octave == (
        PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
    )

    conductor = _rearm_conductor_for_persist(
        "cap_pinned_round", {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY},
        topology_prescription=accepted,
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state()
    assert state["verify_priors"]["topology_prescription"] == accepted.to_dict()
    rehydrated = v2host.topology_prescription_prior_from_state(state)
    assert rehydrated is not None
    assert rehydrated.to_dict() == accepted.to_dict()
    # ...and an ordinary round still banks nothing, so a reader can tell a
    # pinned candidate from the speaker's commissioned crossover.
    v2host.persist_conductor_state(
        _rearm_conductor_for_persist("cap_ordinary_round", {1: PHASE_VERIFY}),
        failure_code=None,
    )
    assert v2host.topology_prescription_prior_from_state(
        v2host.load_v2_state()
    ) is None


def test_a_pre_envelope_alignment_record_round_trips_through_the_prior(caplog):
    """The retrofit contract, end to end through the real wrapper.

    ``verify_priors.alignment_prescription`` is carried unconditionally
    across a deploy (``persist_conductor_state``), and #2662/#2773 shipped
    writing it days before the version+kind envelope existed, so a live
    speaker can already hold a record naming neither field. Built through the
    dataclass's own ``to_dict()`` with the two envelope keys removed, not
    hand-typed, so this is exactly the shape a prior build wrote.
    """
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        AlignmentPrescription,
    )

    prescription = AlignmentPrescription(
        delay_us=-450.0, basis_delay_us=-405.7,
        basis_artifacts=("captures/xover-series2/landscape.json",),
        basis_note="direct arrival gap, n=33",
        checked_at_fc_hz=FC_HZ, lobe_us=200.0,
    )
    pre_envelope_record = prescription.to_dict()
    del pre_envelope_record["kind"]
    del pre_envelope_record["artifact_schema_version"]

    v2host.save_v2_state({
        "session_id": "cap_pre_envelope_alignment",
        "verify_priors": {"alignment_prescription": pre_envelope_record},
    })
    with caplog.at_level(logging.WARNING):
        rehydrated = v2host.alignment_prescription_prior_from_state(
            v2host.load_v2_state()
        )
    assert rehydrated is not None
    assert rehydrated.delay_us == -450.0
    assert rehydrated.basis_delay_us == -405.7
    # Tolerated, not merely swallowed: no "unreadable" WARNING for the legacy
    # shape, which is what separates "read as absent" from "read as this
    # build's own kind and version 1."
    assert "alignment_prescription_unreadable" not in caplog.text


def test_a_pre_envelope_topology_record_round_trips_through_the_prior(caplog):
    """The topology mirror of the test above — same retrofit, same wrapper
    shape, same reason: ``verify_priors.topology_prescription`` predates this
    envelope by the same three days."""
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        TopologyPrescription,
    )

    pinned = TopologyPrescription(
        fc_hz=2400.0, order=4,
        basis_artifacts=("armloop-first-drive-2026-08/offline-fc-search",),
        basis_note="offline candidate search",
        authority="operator_pinned_no_measured_ranking",
    )
    pre_envelope_record = pinned.to_dict()
    del pre_envelope_record["kind"]
    del pre_envelope_record["artifact_schema_version"]

    v2host.save_v2_state({
        "session_id": "cap_pre_envelope_topology",
        "verify_priors": {"topology_prescription": pre_envelope_record},
    })
    with caplog.at_level(logging.WARNING):
        rehydrated = v2host.topology_prescription_prior_from_state(
            v2host.load_v2_state()
        )
    assert rehydrated is not None
    assert rehydrated.fc_hz == 2400.0
    assert rehydrated.order == 4
    assert "crossover_v2_topology_prescription_unreadable" not in caplog.text


def test_an_inadmissible_pin_refuses_at_the_tap_before_any_side_effect(
    monkeypatch,
):
    """Fail-closed, at the untrusted-input boundary, costing nothing.

    6500 Hz is past the woofer's own declared 6000 Hz ceiling and comfortably
    inside the tweeter's band (which declares from 300 Hz), so the LOWER
    DRIVER'S CEILING is the only bound that can refuse it — a one-reason
    fixture, asserted on the reason CONSTANT rather than on wording no test
    owns.

    The one-reason fixture used to be 5500 Hz, refused by a declared search
    band the two roles intersected to 1000-4000 Hz. #2870 deleted that band, so
    5500 Hz is now admissible — both drivers' hard bands allow it — and the
    fixture moved to the surviving damage stop rather than the deleted nanny.

    "At the tap" is the half that matters operationally: an operator walking a
    tournament must learn at the request, not after a ten-minute measurement
    with a burned capture session behind it. So the refusal is asserted TOGETHER
    with the absence of every side effect the preparer would otherwise leave —
    no evidence bundle (armed to fail in ``_arm_stage_1``, and reachable from
    here: nothing is stopping this run at an earlier tap), and no durable
    session state.

    The pin is otherwise perfectly well-formed — supported order, named
    provenance — so a refusal can only be the corner.
    """
    from jasper.active_speaker.crossover_v2.fc_sweep import (
        FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    )

    _arm_stage_1(monkeypatch)
    assert v2host.load_v2_state() is None

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {"topology_prescription": _topology_pin(fc_hz=6500.0)},
            status={}, run_async=None, camilla_factory=None,
        )

    assert FC_REJECT_ABOVE_LOWER_DRIVER_BAND in str(excinfo.value)
    # Never clamped to the nearest legal corner and quietly measured: the
    # operator asked for a candidate, and a silently different candidate is
    # worse than none because its receipt would carry the candidate's name.
    assert "6500" in str(excinfo.value)
    assert v2host.load_v2_state() is None


def test_stage_2_reopens_at_the_topology_the_round_was_measured_at(monkeypatch):
    """The grading stage must re-point too, and this is the half that matters.

    Stage 2 GRADES the applied graph. A pinned round applied a crossover the
    saved declaration does not name, so a stage 2 that opened at the incumbent
    corner would hand VERIFY the wrong design target (R18's absolute claim) and
    the wrong overlap band — the round would be graded for not being the
    crossover it deliberately replaced, and every number would look like a
    realization defect.

    Tapped at ``apply_topology_pin`` because that call IS the re-point — the
    one decision both stages take, owned by the module that owns the pin. The
    rehydration test above proves the record survives the round trip; this
    proves the record is then USED, which is a different claim and the one a
    lossless-but-ignored record would still pass.
    """
    from jasper.active_speaker.crossover_v2 import (
        topology_prescription as topology_mod,
    )

    v2host.set_volume_plan_for_tests(SimpleNamespace(needs_recovery=False))
    monkeypatch.setattr(
        v2host, "resolve_conductor_context", lambda _status: _pinnable_context(),
    )
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda *_a: (SimpleNamespace(), "bundle-stage2-pin"),
    )
    v2host.save_v2_state({
        "session_id": "cap_applied_pinned_round",
        "applied": True,
        "tier": "",
        "verify_priors": {"topology_prescription": _topology_pin()},
    })

    seen: dict[str, Any] = {}
    real = topology_mod.apply_topology_pin

    def _apply(prescription, *, preset, fc_hz):
        # Run the REAL helper first, so a pin it refused to move would be
        # caught here rather than masked by the sentinel.
        moved_preset, moved_fc = real(prescription, preset=preset, fc_hz=fc_hz)
        region = moved_preset.crossover_regions[0]
        seen["fc_hz"], seen["order"] = moved_fc, region.order
        seen["region_fc_hz"] = region.fc_hz
        raise _StoppedAtTheTap("stage 2 re-pointed at the pin")

    monkeypatch.setattr(topology_mod, "apply_topology_pin", _apply)

    with pytest.raises(_StoppedAtTheTap):
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None,
            verify_only=True,
        )

    assert seen["fc_hz"] == _PIN_FC_HZ
    # The PRESET moved too, not just the scalar: the graph VERIFY grades is
    # this topology's, corner and order both.
    assert seen["region_fc_hz"] == _PIN_FC_HZ
    assert seen["order"] == _PIN_ORDER
    # …and the incumbent is a different number, so this cannot pass by accident.
    assert FC_HZ != _PIN_FC_HZ


def test_stage_2_of_an_unpinned_round_re_points_nothing(monkeypatch):
    """The control for the test above, and the reason its tap is honest.

    An ordinary round's stage 2 must open at the speaker's own commissioned
    corner, exactly as it always has. Without this, a stage 2 that re-cornered
    unconditionally would still pass the pinned assertion above while quietly
    rewriting every ordinary round's preset.

    Asserted on what the helper RETURNS rather than on whether it was called:
    ``apply_topology_pin`` is called on every round by design — it is the one
    place absence is turned into "change nothing" — so "was it called" would be
    a test of the wiring's shape instead of its answer.
    """
    from jasper.active_speaker.crossover_v2 import (
        topology_prescription as topology_mod,
    )

    v2host.set_volume_plan_for_tests(SimpleNamespace(needs_recovery=False))
    monkeypatch.setattr(
        v2host, "resolve_conductor_context", lambda _status: _pinnable_context(),
    )
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    # A working stub, not a fail-arm: the bundle opens BEFORE the re-point in
    # the verify-only prepare, so arming it to fail would stop this run short
    # of the seam it is about.
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda *_a: (SimpleNamespace(), "bundle-stage2-ordinary"),
    )
    v2host.save_v2_state({
        "session_id": "cap_applied_ordinary_round",
        "applied": True,
        "tier": "",
        "verify_priors": {},
    })

    seen: dict[str, Any] = {}
    real = topology_mod.apply_topology_pin

    def _apply(prescription, *, preset, fc_hz):
        moved_preset, moved_fc = real(prescription, preset=preset, fc_hz=fc_hz)
        seen["prescription"] = prescription
        seen["preset_unchanged"] = moved_preset is preset
        seen["fc_hz"] = moved_fc
        raise _StoppedAtTheTap("stage 2 resolved its topology")

    monkeypatch.setattr(topology_mod, "apply_topology_pin", _apply)

    with pytest.raises(_StoppedAtTheTap):
        v2host.prepare_v2_session(
            {}, status={}, run_async=None, camilla_factory=None,
            verify_only=True,
        )

    assert seen["prescription"] is None
    # The SAME preset object back, not an equal copy: an unpinned round does
    # not rebuild its crossover regions at all.
    assert seen["preset_unchanged"] is True
    assert seen["fc_hz"] == FC_HZ


def test_the_ceiling_defers_under_a_live_claim_and_offers_no_recovery(monkeypatch):
    """B1 at the host: the wall-clock ceiling fires on a LIVE session.

    ``_enforce_session_volume_ceiling`` exists for the slow-but-alive
    positioner, so it runs on the request thread while a ``TuningSession``
    still holds the claim. The owner records the household level behind that
    claim and lands it on release, so this must read as DEFERRED — zero fader
    writes, nothing latched, and no recovery screen for a household whose
    session is simply still running.
    """
    plan, cam, _claim, clock = _live_measurement_session(monkeypatch)
    clock[0] += 3600.0  # walked away, well past the ceiling
    writes_before = cam.vol

    drained = v2host.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    )

    assert drained is True, "the ceiling still reports that it expired"
    assert cam.vol == writes_before, "the drain moved a fader it does not own"
    assert plan.needs_recovery is False, "a live session is not a recovery case"
    assert plan.unresolved_volume_safety is None, "nothing latched"
    v2host.set_volume_plan_for_tests(None)
