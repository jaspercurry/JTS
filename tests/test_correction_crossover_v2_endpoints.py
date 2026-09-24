# SPDX-FileCopyrightText: 2026 Jasper Curry
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

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_grade as v2grade
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

from tests.engine_twin import retained_take_writer

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from jasper.active_speaker.bundles import CAPTURE_KIND_SEQUENTIAL
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.active_speaker.crossover_v2.conductor_context import V2ConductorContext
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_REVIEW,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    V2_FIRST_BEGIN_TIMEOUT_S,
    build_v2_cloud_index_phase_map,
    build_inline_session_spec,
    LATERAL_MARK_PROMPT,
    v2_first_begin_timeout_s,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams, V2RecordPublishers
from jasper.active_speaker import crossover_envelope_v2 as v2projection
from jasper.active_speaker import baseline_profile, seat_level_reference

import jasper.capture_protocol as capture_protocol
from jasper.capture_protocol import MAX_TTL_S
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_capture, correction_crossover_backend, correction_runtime, correction_setup
from jasper.web import correction_crossover_v2_apply as v2apply
from jasper.active_speaker.crossover_v2.conductor_context import ensure_crossover_preview_ready
from jasper.web import correction_crossover_v2_status as v2status
from jasper.web.correction_crossover_v2_wired import WiredCaptureAnswer

from tests._log_events import event_fields, event_records
from tests.conftest import seat_process_volume_owner
from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    SESSION_VOLUME_DB,
    _preset,
    _roles,
)
from jasper.output_topology_store import save_output_topology, load_output_topology

_BINDING = "placement_abcdefghijklmnopqrstuv"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE", str(tmp_path / "baseline_profile.json"))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH",
        str(tmp_path / "model_error.json"),
    )
    yield
    v2state.set_state_path_for_tests(None)
    v2volume.set_volume_plan_for_tests(None)


def _bg_run_async(coro, *, timeout=None):
    """Mimic correction_runtime.run_async for the host recovery helpers: run the
    coroutine to completion and return its result (each on a fresh loop — the
    session-volume drains are self-contained, no cross-loop context manager)."""
    return asyncio.run(coro)


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
    ceiling_s: float = 10.0,
):
    """A plan holding a LIVE rank-1 claim over a real owner, as a drain finds it.

    Every out-of-runner drain scenario needs the same four things standing up
    together — a real ``VolumeOwner`` over a fake fader, a genuinely held
    ``MeasurementVolumeClaim``, an opened plan, and that plan installed as the
    host's — because a double that merely LOOKS held exercises the no-claim
    door instead of the drain.

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
    cam = _FakeVolCam(-15.0)
    _own_the_fader(monkeypatch, cam)
    owner = volume_owner()
    claim = MeasurementVolumeClaim(owner)
    opened = asyncio.run(
        plan.open(
            -20.0,
            OwnerVolumeDoor(
                owner, read_fader=cam.get_volume_db, claim=claim,
            ),
        )
    )
    assert opened is SessionVolumeOpenResult.OPENED
    assert cam.vol == -20.0
    v2volume.set_volume_plan_for_tests(plan)
    return plan, cam, claim, clock


def _bank_for_apply(raw):
    from jasper.active_speaker.bundles import sessions_dir

    if "candidate" in raw:
        candidate = raw["candidate"]
        path = sessions_dir() / f"authored-{candidate['fingerprint']}" / "evidence/v1/artifacts/crossover_v2/authored/candidate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate))


def _apply(raw, run_async, camilla_factory, *, status=None):
    _bank_for_apply(raw)
    return v2apply.handle_v2_apply(raw, run_async, camilla_factory)


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
    bank = retained_take_writer(store, "cap_retake_session", asyncio.run)

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
    bank = retained_take_writer(store, "cap_record_session", asyncio.run)

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


def _bundle_store(tmp_path):
    """A real bundle and its real evidence store."""
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
    return CommissioningEvidenceStore.open(
        Path(info["bundle_dir"]), expected_session_id=info["session_id"]
    )


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

    store = _bundle_store(tmp_path)
    bundle_dir = Path(store.bundle_dir)
    bank = retained_take_writer(store, "cap_kind_session", asyncio.run)

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
            "session_id": "cap_kind_session",
            "wav_sha256": "d" * 64,
        },
    )

    info = json.loads((bundle_dir / "info.json").read_text())
    # Every kind here shares one list — the recorded kind is the only thing
    # that tells the three apart, which is why it is written at all.
    assert [e["kind"] for e in info["summed_captures"]] == [expected_kind]
    assert info["captures"] == []


@pytest.mark.parametrize("banked", [True, False])
def test_status_publishes_the_banked_seat_level_once(banked, request):
    if banked:
        request.getfixturevalue("banked_session_level")
    with patch.object(
        seat_level_reference, "load_seat_level_reference",
        wraps=seat_level_reference.load_seat_level_reference,
    ) as load:
        block = v2status.crossover_v2_status_block()
    assert block["level"] == (
        {"seat_level_reference_volume_db": -20.0, "leveled_db_spl": 75.0}
        if banked else None
    )
    load.assert_called_once()


def test_state_cloud_block_is_the_compact_projection_of_the_durable_pipeline():
    """PR-4's ``/state`` surface: per band, only ``within_target``; the
    excluded-interval COUNT, not the intervals; the geometry verdict's two
    household-relevant bits. The full per-null τ/r/evidence numbers stay in
    the durable state's own ``pipeline`` sub-key (not re-derived here) and
    the bundle artifact — this is the dashboard-sized read, not a third
    owner of the same data."""
    v2state.save_v2_state({
        "session_id": "cap_state",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
                "positions": [],
                "pipeline": {
                    "available": True,
                    "merged_excluded_bands_hz": [[8000.0, 9000.0], [11000.0, 12000.0]],
                    "spec": {
                        "overall_within_target": False,
                        "reference_db": -27.27,
                        "bands": [
                            {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "within_target": True,
                             "graded_lo_hz": 357.14, "graded_hi_hz": 2000.0,
                             "max_deviation_db": 1.02, "max_deviation_hz": 412.0,
                             "tolerance_db": 1.5},
                            {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "within_target": True,
                             "graded_lo_hz": 2000.0, "graded_hi_hz": 8000.0,
                             "max_deviation_db": -1.41, "max_deviation_hz": 5100.0,
                             "tolerance_db": 2.0},
                            # The top band graded past its NOMINAL 16 kHz edge:
                            # this session's microphone is trusted to 20 kHz.
                            {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "within_target": False,
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
    assert measure["overall_within_target"] is False
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
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "within_target": True,
         "graded_lo_hz": 357.14, "graded_hi_hz": 2000.0,
         "max_deviation_db": 1.02, "max_deviation_hz": 412.0,
         "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "within_target": True,
         "graded_lo_hz": 2000.0, "graded_hi_hz": 8000.0,
         "max_deviation_db": -1.41, "max_deviation_hz": 5100.0,
         "tolerance_db": 2.0},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "within_target": False,
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
    assert verify["overall_within_target"] is None
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
    :func:`household_findings_status` already guards (the ``10 ** 400``
    case in ``test_an_unusable_clock_becomes_none_and_never_takes_the_row_with_it``
    below); ``_finite`` — read here through ``spec.reference_db``, the
    exact path PR #2242's review found it unreachable-but-real on — now
    catches it too.
    """
    v2state.save_v2_state({
        "session_id": "cap_overflow",
        "cloud": {
            PHASE_CLOUD_MEASURE: {
                "geometry": {"locked": True, "reason": "geometry_locked", "thin_evidence": False},
                "positions": [],
                "pipeline": {
                    "available": True,
                    "merged_excluded_bands_hz": [],
                    "spec": {
                        "overall_within_target": True,
                        "reference_db": 10 ** 400,
                        "bands": [],
                    },
                },
            },
        },
    })

    measure = v2status.crossover_v2_status_block()["cloud"][PHASE_CLOUD_MEASURE]
    assert measure["reference_db"] is None
    assert measure["overall_within_target"] is True


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
    v2state.save_v2_state({
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
    assert measure["overall_within_target"] is None
    assert measure["spec_bands"] == []
    assert measure["geometry_guidance"] == (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


def test_state_cloud_block_is_none_before_any_group_closes():
    v2state.save_v2_state({"session_id": "cap_fresh"})
    assert v2status.crossover_v2_status_block()["cloud"] is None


def test_provenance_note_reflects_whether_the_group_matches_the_active_session():
    """The household-facing half of the same marker
    (``compact_cloud_status``'s ``provenance_note``, PR-7). Three states,
    told apart rather than collapsed: the stamped producer matches the
    caller's current session (nothing to say — the chart is fresh); it
    disagrees (a group carried forward from an earlier session — say so);
    or there is no stamp at all (a durable state written before this marker
    existed — unknown, not stale, so an upgrade cannot manufacture a false
    warning for data nobody ever mis-attributed)."""
    pipeline = {"available": True, "spec": {"overall_within_target": True, "bands": []}}
    stamped_state = {
        PHASE_CLOUD_VERIFY: {
            "geometry": {"locked": False},
            "positions": [],
            "pipeline": pipeline,
            "session_id": "cap_producer_session",
        },
    }

    fresh = v2projection.compact_cloud_status(
        stamped_state, current_session_id="cap_producer_session",
    )
    assert fresh[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    stale = v2projection.compact_cloud_status(
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
    legacy = v2projection.compact_cloud_status(
        legacy_state, current_session_id="cap_rearm_session",
    )
    assert legacy[PHASE_CLOUD_VERIFY]["provenance_note"] == ""

    # Backward compatibility: an existing caller that never passes
    # current_session_id at all (every test seam before this PR) still gets
    # the honest "unknown" reading, not a crash or a fabricated verdict.
    no_current = v2projection.compact_cloud_status(stamped_state)
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
    from jasper.cli.doctor import correction
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
                    "overall_within_target": False,
                    "bands": [{"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "within_target": False}],
                },
            },
        },
    }
    v2state.save_v2_state({
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
            analyze=lambda *a, **k: None,
            records=V2RecordPublishers(check=lambda *a, **k: None),
        ),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        index_phase_map={1: PHASE_VERIFY},
    )
    v2state.persist_conductor_state(
        conductor, failure_code=None, evidence={"bundle_session_id": "bundle-2"},
    )

    # Surface 1: the durable state itself.
    state = v2state.load_v2_state()
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
    assert compact[PHASE_CLOUD_MEASURE]["overall_within_target"] is False

    # Surface 3: the envelope.
    monkeypatch.setattr(
        v2volume, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )
    status = {
        "active": True,
        "capture": {"status": "awaiting_capture"},
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    }
    envelope = build_crossover_envelope_v2(status)
    assert envelope["cloud"] is not None
    assert envelope["cloud"][PHASE_CLOUD_MEASURE]["geometry_locked"] is True

    # Surface 4 (named "all three" in the review, the doctor makes four):
    # the doctor no longer reports "no cloud-measurement session recorded".
    monkeypatch.setattr(v2state, "load_v2_state", lambda: state)
    r = check_crossover_v2_cloud_pipeline()
    # A recorded cloud_measure entry means the check no longer takes its
    # REASON_CLOUD_NOT_RUN "nothing recorded yet" branch; with no
    # cloud_verify present the spec-fail does not gate, so this stays ok.
    # The row also folds in the applied-grade finding: this rearm's state IS
    # applied but has no VERIFY outcome yet, which the fold-in now discloses
    # rather than staying silent about (the gap the row's own docstring
    # names).
    assert r.status == "ok"
    assert r.reason == correction.REASON_APPLIED_GRADE_NEVER_GRADED


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
    v2state.save_v2_state({
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
                "pipeline": {"available": True, "spec": {"overall_within_target": False}},
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
            analyze=lambda *a, **k: None,
            records=V2RecordPublishers(check=lambda *a, **k: None),
        ),
        driver_spacing_m=0.15,
        accepted_phases=(),
        applied=False,
        index_phase_map={1: PHASE_CLOUD_MEASURE},
    )
    v2state.persist_conductor_state(conductor, failure_code=None, evidence=None)

    state = v2state.load_v2_state()
    assert state["session_id"] == "cap_fresh_session"
    # Honestly None -- "this session has not closed a group yet" -- never
    # the previous session's stale verdict.
    assert state["cloud"] is None
    assert v2status.crossover_v2_status_block()["cloud"] is None


def _seeded_session_with_a_banked_finding(copy: str) -> None:
    """A completed measuring session whose fit banked one household finding."""
    v2state.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "evidence": {
            "bundle_session_id": "bundle-stage-1",
            v2durable.FINDING_HOUSEHOLD_REFS_KEY: [
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
            analyze=lambda *a, **k: None,
            records=V2RecordPublishers(check=lambda *a, **k: None),
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
    v2state.save_v2_state({
        "session_id": "cap_measuring_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-measured"},
        "applied": True,
        "fc_selection": legacy,
    })

    v2state.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
    )
    assert "fc_selection" not in (v2state.load_v2_state() or {})

    # The same on the measuring side of the seam — no route writes the key.
    v2state.persist_conductor_state(
        _rearm_conductor(
            "cap_fresh_measure",
            index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE},
        ),
        failure_code=None,
    )
    assert "fc_selection" not in (v2state.load_v2_state() or {})


_FINDING_COPY = "Two measurements of how this speaker's ranges balance disagreed."
_RIPPLE_RESERVATION = {"predicted_ripple_db": 15.244, "threshold_db": 15.0}


def _seeded_session_with_a_reservation(measure: dict) -> None:
    """A completed measuring session whose accepted MEASURE banked one."""
    v2state.save_v2_state({
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
    ("seed", "state_path", "status_path", "expected"),
    (
        pytest.param(
            lambda: _seeded_session_with_a_banked_finding(_FINDING_COPY),
            ("evidence", v2durable.FINDING_HOUSEHOLD_REFS_KEY, 0, "household_copy"),
            ("findings", 0, "household_copy"),
            _FINDING_COPY,
            id="banked-finding",
        ),
        pytest.param(
            lambda: _seeded_session_with_a_reservation(
                {"ripple_reservation": _RIPPLE_RESERVATION}),
            ("measure", "ripple_reservation"),
            ("measure", "ripple_reservation"),
            _RIPPLE_RESERVATION,
            id="ripple-reservation",
        ),
        pytest.param(
            lambda: _seeded_session_with_a_reservation(
                {"calibration_reservation": True}),
            ("measure", "calibration_reservation"),
            ("measure", "calibration_reservation"),
            True,
            id="mic-calibration-reservation",
        ),
    ),
)
def test_stage_2_keeps_what_the_measuring_session_disclosed(
    seed, state_path, status_path, expected,
):
    """Walks the real seam: seeded durable state -> the REAL re-arm conductor
    -> the REAL ``persist_conductor_state`` -> the three surfaces the
    disclosure has to reach (durable state, ``/state``, the done screen).
    """
    seed()
    v2state.persist_conductor_state(
        _rearm_conductor("cap_rearm_session", index_phase_map={1: PHASE_VERIFY}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-stage-2"},
    )

    # Surface 1: the durable state — carried across the bundle hop.
    state = v2state.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["evidence"]["bundle_session_id"] == "bundle-stage-2"
    assert _dig(state, state_path) == expected

    # Surface 2: /state's projection.
    status = v2status.crossover_v2_status_block()
    assert _dig(status, status_path) == expected


@pytest.mark.parametrize(
    ("seed", "state_path", "cleared_state", "status_path", "cleared_status"),
    (
        pytest.param(
            lambda: _seeded_session_with_a_banked_finding(
                "An old finding nobody re-measured."),
            ("evidence", v2durable.FINDING_HOUSEHOLD_REFS_KEY),
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
    v2state.persist_conductor_state(
        _rearm_conductor("cap_fresh_session", index_phase_map={1: PHASE_MEASURE}),
        failure_code=None,
        evidence={"bundle_session_id": "bundle-fresh"},
    )

    assert _dig(v2state.load_v2_state(), state_path, missing=_ABSENT) is cleared_state
    assert _dig(v2status.crossover_v2_status_block(), status_path) == cleared_status


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
    v2state.save_v2_state({"session_id": "cap_placeholder"})
    path = Path(v2state._state_path())
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
            v2durable.FINDING_HOUSEHOLD_REFS_KEY: rows,
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
    read.
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
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_MEASURE

    # A partially-recognisable list keeps only what it can name — and that IS
    # enough to walk, so it is used rather than discarded.
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": ["nonsense", PHASE_VERIFY],
        "applied": True,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_VERIFY


def test_apply_completes_a_plan_without_verify():
    v2state.save_v2_state({
        "session_id": "cap_x", "applied": False,
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
    })
    v2state.observe_apply_success("candidate")
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_DONE


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
        v2state.save_v2_state({
            "session_id": "cap_x",
            "accepted_phases": list(phases),
            "session_phases": list(phases),
            "applied": True,
        })
        assert v2status.crossover_v2_status_block()["phase"] == PHASE_DONE, phases


def test_a_measured_fallback_walk_waits_for_review_without_a_candidate():
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "session_phases": ["nonsense", "also-not-a-phase"],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_REVIEW


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
            analyze=lambda *a, **k: None,
            records=V2RecordPublishers(check=lambda *a, **k: None),
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
    v2state.save_v2_state({
        "session_id": "cap_original_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "verify_priors": {"pilot_transfer_reference": reference},
    })
    conductor = _rearm_conductor_for_persist(
        "cap_rearm_session", {1: PHASE_VERIFY},
    )
    v2state.persist_conductor_state(conductor, failure_code=None)

    state = v2state.load_v2_state()
    assert state["session_id"] == "cap_rearm_session"
    assert state["verify_priors"]["pilot_transfer_reference"] == reference


def test_a_measuring_session_drops_the_prior_level_reference():
    """A pilot transfer is captured THROUGH the applied graph, so once a new
    candidate is measured the previous reference answers a different question.
    A measuring session drops it rather than letting the next stage-2 verify
    report a graph change as a level-reference move — the misattribution
    #1924 and #1927 both exist to stop."""
    v2state.save_v2_state({
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
    v2state.persist_conductor_state(conductor, failure_code=None)

    state = v2state.load_v2_state()
    assert state["verify_priors"]["pilot_transfer_reference"] is None


def test_prepare_refuses_when_volume_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2volume.set_volume_plan_for_tests(_NeedsRecovery())
    with pytest.raises(refusal_copy.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            _inline_body(), status={}, run_async=None, camilla_factory=None
        )
    assert excinfo.value.code == refusal_copy.REASON_VOLUME_UNRESOLVED
    assert correction_runtime.refusal_envelope(excinfo.value)["next_action"]["id"] == "recover_volume"


@pytest.mark.parametrize("body", [{}, {"tier": "full"}, {"stage": "post_apply"}, {"plan": {}}])
def test_session_requires_an_inline_v3_plan(body):
    from jasper.web.correction_runtime import refusal_envelope
    with pytest.raises(refusal_copy.CrossoverV2Refused) as caught:
        v2host.prepare_v2_session(body, status={}, run_async=None, camilla_factory=None)
    envelope = refusal_envelope(caught.value)
    assert envelope["code"] in {"program_plan_shape_invalid", "walk_schema_version_unsupported"}
    assert envelope["next_action"]


def test_session_open_refuses_the_preflight_candidate_code(monkeypatch):
    from jasper.active_speaker import preflight_live
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, REGIME_SUMMED
    from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
    from tests.test_preflight import ready_facts

    name = "unbanked"
    request = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, candidate_id=name),), candidates=(name,))
    v2volume.set_volume_plan_for_tests(SimpleNamespace(needs_recovery=False))
    monkeypatch.setattr(v2host, "resolve_conductor_context", lambda _: SimpleNamespace(
        safety_profile={"targets": []}, role_targets={}, preset=_preset(), topology=object(),
    ))
    monkeypatch.setattr(v2evidence, "open_v2_evidence_store", lambda *_: pytest.fail("bundle opened before preflight"))
    monkeypatch.setattr(v2host, "_resolve_prepare_wired_mic", lambda: object())
    monkeypatch.setattr(preflight_live, "read_preflight_facts", lambda *args, **kwargs: ready_facts(
        request,
    ))
    with pytest.raises(refusal_copy.CrossoverV2Refused) as exc:
        v2host.prepare_v2_session({"plan": request.to_dict()}, status={}, run_async=None, camilla_factory=None)
    assert exc.value.code == "not_found"
    assert refusal_copy_for(exc.value.code)[1]


def test_prepare_refuses_unrepresentable_confirmed_protection_before_bundle(
    monkeypatch,
):
    class _Ready:
        needs_recovery = False

    def _unrepresentable(*_args):
        raise ValueError("unsupported confirmed filter")

    from jasper.active_speaker import branch_chain

    _ready_inline(monkeypatch)
    v2volume.set_volume_plan_for_tests(_Ready())
    monkeypatch.setattr(
        v2host, "resolve_conductor_context",
        lambda _status: _inline_context(),
    )
    monkeypatch.setattr(branch_chain, "confirmed_protection_sections", _unrepresentable)
    monkeypatch.setattr(
        v2evidence, "open_v2_evidence_store",
        lambda *_: pytest.fail("bundle opened before protection preflight"),
    )
    with pytest.raises(refusal_copy.CrossoverV2Refused) as refused:
        v2host.prepare_v2_session(_inline_body(), status={}, run_async=None, camilla_factory=None)
    assert refused.value.code == "driver_protection_invalid"
    assert correction_runtime.refusal_envelope(refused.value)["next_action"]["id"] == "review_safety_limits"


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
    ever reached this function historically overshot its OWN cap
    (``_decimate_sum``'s old raw stride landed at/above 512-513 for a real
    capture). #1858's block-average fix
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
    ``test_a_realized_prediction_stays_within_the_chart_cap``)
    over 1..5000 plus 2000 random larger lengths: max observed output was
    exactly 256, never more, for any input."""
    n = v2projection.CHART_CURVE_MAX_JSON_POINTS * 4 + 7  # not a multiple of the cap
    freqs = [100.0 + i for i in range(n)]
    mags = [float(i % 5) for i in range(n)]
    raw = {"freqs_hz": freqs, "magnitude_db": mags}

    v2state.save_v2_state({
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
    stride = -(-n // v2projection.CHART_CURVE_MAX_JSON_POINTS)
    assert stride == 5
    assert len(predicted["freqs_hz"]) == len(range(0, n, stride))
    assert len(predicted["freqs_hz"]) == 207
    # The hard ceiling itself: never CAP + stride (the old soft promise),
    # always CAP outright.
    assert len(predicted["freqs_hz"]) <= v2projection.CHART_CURVE_MAX_JSON_POINTS


def test_a_realized_prediction_stays_within_the_chart_cap():
    """Gate finding on #1858 (SF-1): pin the REALIZED wire length, not the
    constants that feed it. The persist-time decimator (``_decimate_sum``)
    and the chart-time re-decimation (``decimate_curve_for_chart``) are driven
    at real FFT-bin grid sizes — the 65536- and 16384-point windows' 32769-
    and 8193-bin grids — so the bound is a property of the functions, not of
    one fixture that happens to clear it.
    """
    for n_fft in (1 << 16, 1 << 14):
        freqs = np.fft.rfftfreq(n_fft, 1.0 / 48000.0)
        persisted = v2durable._decimate_sum((freqs, np.zeros(freqs.size)))
        rendered = v2projection.decimate_curve_for_chart(
            persisted["freqs_hz"], persisted["magnitude_db"],
        )
        assert len(rendered["freqs_hz"]) <= v2projection.CHART_CURVE_MAX_JSON_POINTS


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

    decimated = v2durable._decimate_sum((freqs, mag_db))
    out_freqs = np.asarray(decimated["freqs_hz"])
    out_mag = np.asarray(decimated["magnitude_db"])
    assert len(out_freqs) <= v2durable.MAX_PERSISTED_SUM_POINTS
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
        freqs, mag_db, v2durable.MAX_PERSISTED_SUM_POINTS,
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
    evaluator refused) ⇒ the curve with ``overall_within_target`` **None** and no
    bands — never ``False``, which would read as a measured failure, and never
    ``True``, which the compact-cloud rule already forbids fabricating."""
    v2state.save_v2_state({"session_id": "cap_x", "verify_priors": None})
    assert v2status.crossover_v2_status_block()["prediction"] is None

    v2state.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {"predicted_sum": None, "predicted_spec": None},
    })
    assert v2status.crossover_v2_status_block()["prediction"] is None

    v2state.save_v2_state({
        "session_id": "cap_x",
        "verify_priors": {
            "predicted_sum": {"freqs_hz": [100.0, 200.0], "magnitude_db": [0.0, 0.0]},
            "predicted_spec": None,
        },
    })
    prediction = v2status.crossover_v2_status_block()["prediction"]
    assert prediction["curve"]["freqs_hz"] == [100.0, 200.0]
    assert prediction["overall_within_target"] is None
    assert prediction["spec_bands"] == []
    assert prediction["reference_db"] is None


def test_observe_apply_success_marks_the_state_applied():
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    v2state.observe_apply_success("fp-1")
    assert v2state.load_v2_state()["applied"] is True


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
    v2state.save_v2_state({"session_id": "cap_ok", "applied": False})
    good = v2state.load_v2_state()

    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            v2state.save_v2_state({
                "session_id": "cap_bad",
                "verify": {"claims": {"residual_db": bad}},
            })
        assert v2state.load_v2_state() == good


def test_observe_apply_success_records_the_way_back_pointer():
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "candidate": {"fingerprint": "fp-1"},
        "applied": False,
    })
    v2state.observe_apply_success("fp-1", previous_candidate_fingerprint="fp-prev")
    assert v2state.load_v2_state()["previous_candidate_fingerprint"] == "fp-prev"
    # The speaker's first-ever apply has nothing to point back to, and a
    # later apply that displaced a non-measured profile clears the pointer.
    v2state.observe_apply_success("fp-1", previous_candidate_fingerprint=None)
    assert v2state.load_v2_state()["previous_candidate_fingerprint"] is None


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
    }
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY],
        "applied": True,
        "attempts_loop": loop,
    })

    from jasper.active_speaker.model_error_store import (
        MODEL_ERROR_STATE_KIND,
        model_error_state_path,
    )

    model_error_state_path().write_text(json.dumps({
        "kind": MODEL_ERROR_STATE_KIND,
        "model_error": [{"attempt_id": f"candidate-{index}"} for index in range(7)],
    }))

    block = v2status.crossover_v2_status_block()
    assert block["attempts_loop"] == {
        "store_count": 7,
    }
    assert "history" not in block["attempts_loop"]

    v2state.reset_v2_journey_state()
    assert v2state.load_v2_state()["attempts_loop"] == loop


def test_status_block_reports_an_applied_but_ungraded_result():
    """PR-L4 item 4: applied implies graded, and when it does not, `/state`
    says so in its own field rather than leaving an empty `verify` block for
    every surface to read as "nothing to report" — which is how a 10 dB-dark
    profile sat on JTS3 under a green tick."""
    v2state.save_v2_state({
        "session_id": "cap_ungraded",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
    })
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2grade.GRADE_UNVERIFIED
    assert grade["graded"] is False
    assert grade["verify_outcome"] is None


def test_status_block_reports_a_graded_result_from_either_instrument():
    """Both a passing VERIFY and a graded post-apply cloud are real checks, and
    the tiers differ in which one they run — express omits the post-apply group
    entirely, so keying only on the cloud would call every express session
    ungraded."""
    v2state.save_v2_state({
        "session_id": "cap_graded_verify",
        "applied": True,
        "verify": {"outcome": "pass"},
    })
    by_verify = v2status.crossover_v2_status_block()["post_apply_grade"]
    # Verified at the mark only — express's whole grade, and distinguishable
    # from a walked post-apply group WITHOUT consulting `tier`.
    assert by_verify["state"] == v2grade.GRADE_MARK_VERIFIED
    assert by_verify["graded"] is True

    # The cloud instrument grading ALONE. #2464 moved this fixture off
    # ``outcome="inconclusive"`` — that pairing now grades ``inconclusive``,
    # and pinning it here pinned the mask instead of the claim this test
    # makes. A session whose VERIFY produced no outcome at all is the honest
    # way to ask "does a closed group grade on its own".
    v2state.save_v2_state({
        "session_id": "cap_graded_cloud",
        "applied": True,
        "cloud": {
            PHASE_CLOUD_VERIFY: {
                "geometry": {"locked": False},
                "pipeline": {
                    "available": True,
                    "spec": {"overall_within_target": False, "bands": []},
                    "merged_excluded_bands_hz": [],
                },
                "session_id": "cap_graded_cloud",
            },
        },
    })
    by_cloud = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert by_cloud["state"] == v2grade.GRADE_GRADED
    # A grade that exists and FAILED is still a grade — "we checked and it is
    # out of spec" is a different claim from "we never checked", and item 7's
    # headline is what renders the first one.
    assert by_cloud["post_apply_spec_passed"] is False


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
            "overall_within_target": False, "bands": [],
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
    v2state.save_v2_state(_honest_result_state(**changes))
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
    from jasper.active_speaker.crossover_v2.capture_plan import (  # lazy: avoid measurement-stack import cost on unused paths
        STAGE1_INCLUDES_ENTRY_BASELINE,
    )

    stage1 = list(dict.fromkeys(build_v2_cloud_index_phase_map(


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
            "overall_within_target": False, "bands": [],
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

    # This test is specifically about the SHIPPED shape, so it says so here
    # rather than in the shared fixture: stage 1 walks no poses, which is
    # exactly why no sweep runs and no selection is banked.
    state = _no_sweep_state()
    assert state["session_phases"][:3] == [
        PHASE_CHECK, PHASE_MEASURE, PHASE_ENTRY_BASELINE,
    ]

    v2state.save_v2_state(state)
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
        v2state.save_v2_state(_no_sweep_state(fc_selection=legacy))
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
        assert (v2state.load_v2_state() or {})["fc_selection"] == legacy


def test_terminal_result_logs_once_with_target_failure_evidence(caplog, monkeypatch):
    prior = _honest_result_state()
    prior["session_phases"] = [PHASE_VERIFY]
    v2state.save_v2_state(prior)

    class TerminalConductor(_StubConductor):
        verify_outcome = "pass"
        verify_claims = prior["verify"]["claims"]
        measure_predicted_spec_report = prior["verify_priors"]["predicted_spec"]

        def snapshot(self):
            return SimpleNamespace(
                session_id="cap_p04", accepted_phases=(PHASE_VERIFY,),
                session_phases=(PHASE_VERIFY,),  applied=True,
                gain_plan_db=None, candidate_fingerprint=None,
            )

    conductor = TerminalConductor("cap_p04")
    from jasper.active_speaker.crossover_v2.durable_state import ConductorState
    monkeypatch.setattr(v2state, "build_conductor_state", lambda *a, **k: ConductorState(
        {**prior, "accepted_phases": [PHASE_VERIFY]}, False))
    with caplog.at_level(logging.INFO, logger=v2state.__name__):
        v2state.persist_conductor_state(conductor, failure_code=None)
        v2state.persist_conductor_state(conductor, failure_code=None)
        v2status.crossover_v2_status_block()
    fields = event_fields(caplog, "correction.crossover_v2_result_classified")
    assert fields["outcome"] == "verified_best_evaluated"
    assert fields["absolute_passed"] == "false"
    assert fields["absolute_miss_db"] == "4.3139"
    assert fields["absolute_worst_hz"] == "1590.4083"
    assert fields["candidate_fingerprint"] == "fp-p04"


def test_terminal_result_log_tolerates_a_malformed_projection(monkeypatch, caplog):
    conductor = _StubConductor("cap_malformed")
    conductor.snapshot = lambda: SimpleNamespace(
        session_id="cap_malformed", accepted_phases=(PHASE_VERIFY,),
        session_phases=(PHASE_VERIFY,), applied=True,
        gain_plan_db=None, candidate_fingerprint=None,
    )
    monkeypatch.setattr(
        v2status, "crossover_v2_status_block", lambda: {"post_apply_grade": None},
    )
    with caplog.at_level(logging.INFO, logger=v2state.__name__):
        v2state.persist_conductor_state(conductor, failure_code=None)
    fields = event_fields(caplog, "correction.crossover_v2_result_classified")
    assert fields["outcome"] == "inconclusive"


_NO_GAUGE = object()  # "this era wrote no flatness key", vs. an explicit None


def _closed_cloud_group(*, passed, flatness=_NO_GAUGE):
    """A closed post-apply group in DURABLE shape, as the conductor writes it."""
    pipeline = {
        "available": True,
        "spec": {"overall_within_target": passed, "bands": []},
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

    ``overall_within_target=False`` reaches ``GRADE_GRADED`` because a
    graded-and-failed group IS graded, and every consuming surface read that
    state name as a clean result: doctor printed ``applied and graded
    (state=graded, verify=pass)`` beside a cloud line reading ``spec=fail
    worst=-4.63dB``. ``state`` cannot carry the difference; ``spatial`` does,
    and the failing gauge's own number rides with it so no consumer re-derives
    it. The ruling is grade-and-disclose: the tune stays, the failure is
    loud."""
    v2state.save_v2_state(_applied_state(
        tier="full",
        cloud_verify=_closed_cloud_group(
            passed=False, flatness=_GRADED_AND_FAILED_FLATNESS,
        ),
    ))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2grade.GRADE_GRADED  # unchanged vocabulary
    assert grade["spatial"] == v2grade.GRADE_SPATIAL_FAILED
    # A failed grade is a COMPLETED grade — the tier delivered what it
    # promised, and what it delivered is a miss.
    assert grade["scope"] == v2grade.GRADE_SCOPE_SPATIAL
    assert grade["complete"] is True
    assert grade["spatial_worst_db"] == pytest.approx(-4.628)
    assert grade["spatial_worst_hz"] == pytest.approx(1650.0)


@pytest.mark.parametrize("storage", ["live", "banked", "recovery"])
@pytest.mark.parametrize("poses,complete", [
    ([{"kind": "bearing", "deg": 0, "elevation_deg": 0}], True),
    ([{"kind": "bearing", "deg": 0}, {"kind": "bearing", "deg": 20}], False),
    ([{"kind": "bearing", "deg": 0, "elevation_deg": 10}], False),
    ([{"kind": "seat", "deg": 0, "seat_offset_m": [0.3, 0, 0]}], False),
])
def test_a_session_whose_plan_asked_beyond_the_mark_is_incomplete_at_the_mark(
    tmp_path, monkeypatch, poses, complete, storage,
):
    import shutil
    from jasper.active_speaker.crossover_contract import REASON_APPLIED_GRADE_MARK_ONLY
    from tests.run_manifest_fixture import write_asked_poses

    state = _applied_state()
    root = write_asked_poses(tmp_path, state, poses)
    monkeypatch.setattr("jasper.active_speaker.grade_coverage.sessions_dir", lambda: root)
    if storage == "banked":
        target = tmp_path / "campaigns" / state["session_id"] / "bundle"
        target.mkdir(parents=True)
        shutil.move(root / "asked-run", target)
    elif storage == "recovery":
        original = root / "asked-run/evidence/v1/artifacts/crossover_v2" / state["session_id"]
        state["candidate"] = {"fingerprint": "applied-candidate"}
        state["session_id"] = "recovery"
        write_asked_poses(tmp_path, state, [{"deg": 0}])
        monkeypatch.setattr("jasper.active_speaker.grade_coverage.load_applied_candidate",
                            lambda fingerprint, **kw: SimpleNamespace(path=original / "candidate.json"))
    v2state.save_v2_state(state)
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["scope"] == v2grade.GRADE_SCOPE_MARK
    assert grade["complete"] is complete
    assert grade.get("reason") == (None if complete else REASON_APPLIED_GRADE_MARK_ONLY)


@pytest.mark.parametrize("initial_manifest", [False, True])
def test_coverage_tracks_live_manifest_arrival_and_bank_moves(tmp_path, monkeypatch, initial_manifest):
    import shutil
    from jasper.active_speaker.grade_coverage import asked_beyond_mark
    from tests.run_manifest_fixture import write_asked_poses

    state = _applied_state()
    root = write_asked_poses(tmp_path, state, [{"deg": 0}])
    if not initial_manifest:
        shutil.rmtree(root / "asked-run" / "evidence")
    monkeypatch.setattr("jasper.active_speaker.grade_coverage.sessions_dir", lambda: root)
    assert asked_beyond_mark(state, applied_profile=None) is False
    write_asked_poses(tmp_path, state, [{"deg": 20}])
    assert asked_beyond_mark(state, applied_profile=None) is True
    target = tmp_path / "campaigns" / state["session_id"] / "bundle"
    target.mkdir(parents=True)
    shutil.move(root / "asked-run", target)
    assert asked_beyond_mark(state, applied_profile=None) is True


_PASSING_GROUP = {"passed": True, "flatness": {
    **_GRADED_AND_FAILED_FLATNESS, "max_db": 0.9, "passed": True,
}}

_UNMEASURABLE_FLATNESS = {
    **_GRADED_AND_FAILED_FLATNESS,
    "max_db": None, "max_hz": None, "evaluable": False, "passed": False,
}

_PASSING = _closed_cloud_group(**_PASSING_GROUP)


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        pytest.param(
            {"tier": "full", "cloud_verify": _PASSING},
            # No number beside a pass: printing the margin of a pass next to a
            # failure verdict is how the two get confused.
            {"spatial": v2grade.GRADE_SPATIAL_PASSED,
             "scope": v2grade.GRADE_SCOPE_SPATIAL, "complete": True,
             "spatial_worst_db": None, "spatial_worst_hz": None},
            id="closed-and-passing-group-is-a-complete-spatial-grade",
        ),
        # Reading an unmeasurable spectrum as a miss states a measurement that
        # never happened. No spatial CLAIM exists, so the delivered width falls
        # back to what the mark proved — on Full, short of the promise.
        pytest.param(
            {"tier": "full", "cloud_verify": _closed_cloud_group(
                passed=False, flatness=_UNMEASURABLE_FLATNESS)},
            {"spatial": v2grade.GRADE_SPATIAL_UNMEASURABLE,
             "spatial_worst_db": None, "scope": v2grade.GRADE_SCOPE_MARK,
             "complete": True},
            id="an-ungradeable-group-is-not-a-failure",
        ),
        # Unmeasurable is claimed only on POSITIVE evidence: a state written
        # before the gauge shipped carries a real ``overall_within_target=False`` and
        # no ``flatness``, and downgrading that on the ABSENCE of an instrument
        # is the fabricated reading pointed the other way.
        pytest.param(
            {"tier": "full", "cloud_verify": _closed_cloud_group(passed=False)},
            {"spatial": v2grade.GRADE_SPATIAL_FAILED, "spatial_worst_db": None},
            id="a-failing-group-with-no-gauge-stays-a-failure",
        ),
        # A pre-tier state file, or one from a later build: this build cannot
        # know what was promised, and manufacturing an incompleteness warning
        # about a promise it never read is worse than saying what it said.
        pytest.param(
            {"tier": None},
            {"scope": v2grade.GRADE_SCOPE_MARK, "complete": True},
            id="an-unreadable-tier-is-judged-on-delivery",
        ),
        pytest.param(
            {"tier": "tier-from-2027"}, {"complete": True},
            id="a-tier-from-the-future-is-judged-on-delivery",
        ),
        pytest.param(
            {"tier": "full", "verify_outcome": "inconclusive"},
            {"state": v2grade.GRADE_INCONCLUSIVE,
             "scope": v2grade.GRADE_SCOPE_NONE, "complete": False},
            id="a-verify-that-did-not-pass-delivers-no-scope",
        ),
        # #2464: a failed or undecided mark-VERIFY caps the badge whatever the
        # group says. ``cloud_verdict`` was tested BEFORE the fail arm, so any
        # closed group masked it. The spatial instrument's own verdict is
        # untouched and still rides its own field (#2160 rider) — capping the
        # badge is not co-locating the two facts.
        pytest.param(
            {"tier": "full", "verify_outcome": "fail",
             "claims": {"integration": {"status": "fail", "max_db": 4.2}},
             "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_FAILED, "graded": False,
             "spatial": v2grade.GRADE_SPATIAL_PASSED,
             "post_apply_spec_passed": True},
            id="a-failed-verify-is-not-masked-by-a-passing-spatial-grade",
        ),
        # ``verify.outcome`` grades capture and tracking health ONLY, so a
        # crossover-region claim that missed its tolerance rides a clean
        # ``pass``: the CLAIMS record is the source, and both facts stand.
        pytest.param(
            {"tier": "full", "verify_outcome": "pass",
             "claims": {"integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "fail", "max_db": 4.31,
                                     "worst_hz": 1590.4}},
             "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_FAILED, "graded": False,
             "verify_outcome": "pass"},
            id="a-failed-absolute-claim-caps-the-badge-on-a-clean-capture",
        ),
        # The two instruments are a UNION, not a fallback: an ``outcome`` fail
        # whose claims are ``not_evaluated`` still caps.
        pytest.param(
            {"tier": "full", "verify_outcome": "fail",
             "claims": {"integration": {"status": "not_evaluated"},
                        "absolute": {"status": "not_evaluated",
                                     "reason": "no_trusted_region"}},
             "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_FAILED},
            id="an-outcome-fail-whose-claims-could-not-grade-still-caps",
        ),
        # The same masking defect one arm over: the ``inconclusive`` arm was
        # unreachable behind the closed-group test.
        pytest.param(
            {"tier": "full", "verify_outcome": "inconclusive",
             "cloud_verify": _closed_cloud_group(passed=False)},
            {"state": v2grade.GRADE_INCONCLUSIVE, "graded": False},
            id="an-inconclusive-verify-is-not-masked-by-a-closed-group",
        ),
        # The cap is scoped to a FAILED or undecided VERIFY. On a clean pass
        # the walked group is the wider claim and still wins the state word,
        # or this demotes every correctly graded Full session.
        pytest.param(
            {"tier": "full", "verify_outcome": "pass",
             "claims": {"integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "pass", "max_db": 0.8}},
             "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_GRADED, "graded": True, "complete": True},
            id="a-clean-pass-still-grades-on-the-wider-spatial-claim",
        ),
        # Absence of claims is a pre-R18 state file, never a fail and never a
        # pass-of-claims: the outcome stands as the only record there is.
        pytest.param(
            {"tier": "full", "verify_outcome": "pass", "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_GRADED},
            id="no-claims-block-graded-on-a-passing-outcome-alone",
        ),
        pytest.param(
            {"tier": "full", "verify_outcome": "fail", "cloud_verify": _PASSING},
            {"state": v2grade.GRADE_FAILED},
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
    v2state.save_v2_state(_applied_state(**state))
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    for key, value in expected.items():
        assert grade[key] == value, key


def test_status_block_never_asks_an_unapplied_session_for_a_grade():
    v2state.save_v2_state({"session_id": "cap_none", "applied": False})
    grade = v2status.crossover_v2_status_block()["post_apply_grade"]
    assert grade["state"] == v2grade.GRADE_NOT_APPLIED
    # Nothing promised, so nothing outstanding: `complete=False` here would
    # warn every speaker that has never been commissioned.
    assert grade["complete"] is True
    assert grade["scope"] == v2grade.GRADE_SCOPE_NONE
    assert grade["spatial"] == v2grade.GRADE_SPATIAL_ABSENT
    assert grade["graded"] is True


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

    curve_sentinel = CalibrationCurve([20.0, 20000.0], [0.0, 1.0])

    class _Record:
        curve = curve_sentinel
        calibration_id = "cal-123"

    resolved: list = []

    def resolver(setup, device):
        resolved.append((setup, device))
        return _Record()

    meta: dict[str, Any] = {}
    analyze = v2evidence.bind_production_analyze(
        resolve_calibration=resolver, meta=meta,
    )
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
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": "cal-123",
        "curve_fingerprint": json_fingerprint(curve_sentinel.to_dict()),
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

    report = {"frames": 4, "encoded_frames": 4, "capture_gaps": 0,
              "capture_gap_frames": 0}
    analyze = v2evidence.bind_production_analyze(
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
    analyze = v2evidence.bind_production_analyze(
        resolve_calibration=lambda setup, device: None, meta=meta
    )
    program = build_verify_program(FC_HZ, sweep_s=0.5)
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2_evidence"):
        analyze(
            program, _FakeResult(), MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    # NOT silent: analysis ran uncalibrated, annotated as a stored fact + WARN.
    assert seen["calibration"] is None
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None, "curve_fingerprint": None}
    # W6.13 round-5 diagnostic: the WARN names what the phone-reported setup
    # actually held at resolve time — here nothing at all.
    fields = event_fields(caplog, "correction.crossover_v2_uncalibrated_capture")
    assert fields["setup_mode"] == "absent"


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
        curve = CalibrationCurve([20.0, 20000.0], [0.0, 1.0])
        calibration_id = "cal-umik2"
        model = "minidsp_umik2"

    analyze = v2evidence.bind_production_analyze(
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
    analyze = v2evidence.bind_production_analyze(
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
    analyze = v2evidence.bind_production_analyze(
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
    analyze = v2evidence.bind_production_analyze(
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
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2_evidence"):
        analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )
    fields = event_fields(caplog, "correction.crossover_v2_uncalibrated_capture")
    assert fields["setup_mode"] == "stored"
    assert fields["setup_calibration_id"] == "cal-stale"
    # Redaction: the serial never reaches the journal.
    assert "SECRET-810" not in caplog.text


def test_setup_calibration_observation_is_redacted_safe():
    """The extractor itself: absent / mode-none / stored shapes, and only
    mode + calibration_id ever come back."""
    assert v2evidence._setup_calibration_observation(None) == ("absent", "")
    assert v2evidence._setup_calibration_observation({}) == ("absent", "")
    assert v2evidence._setup_calibration_observation(
        {"calibration": {"mode": "none"}}
    ) == ("none", "")
    assert v2evidence._setup_calibration_observation(
        {"calibration": {"mode": "stored", "calibration_id": "cal-1"}}
    ) == ("stored", "cal-1")
    assert v2evidence._setup_calibration_observation(
        {"calibration": {"mode": "serial", "serial": "810-8494"}}
    ) == ("serial", "")


def test_production_analyze_default_resolver_is_the_household_mic_owner():
    """The default resolver IS household_mic.resolve_setup_calibration (the one
    point a capture's setup reference becomes a record) — a no-choice setup
    resolves to None."""
    assert v2evidence.resolve_setup_calibration(None, None) is None
    assert v2evidence.resolve_setup_calibration({"calibration": {"mode": "none"}}, None) is None


def _seed_household_mic(tmp_path, monkeypatch):
    """A resolvable stored household mic (mirrors
    test_default_setup_calibration_for_spec_present_and_absent)."""
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.audio_measurement.household_mic import (
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
    hint correction_capture._default_setup_calibration_for_spec builds for
    level_ramp, now available to a v2 session too."""
    assert v2evidence.default_setup_calibration_for_v2() is None

    record = _seed_household_mic(tmp_path, monkeypatch)

    hint = v2evidence.default_setup_calibration_for_v2()
    assert hint is not None
    assert hint.mode == "serial"
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is True


@pytest.mark.parametrize("with_calibration", [False, True])
def test_inline_and_verify_specs_carry_the_default_calibration_hint(
    tmp_path, monkeypatch, with_calibration,
):
    record = _seed_household_mic(tmp_path, monkeypatch)
    hint = v2evidence.default_setup_calibration_for_v2()
    assert hint is not None
    kwargs = {"default_setup_calibration": hint} if with_calibration else {}
    spec = build_inline_session_spec(
        [(MeasureSpec(kind="candidate", program_phase=PHASE_CHECK), LATERAL_MARK_PROMPT, "base")],
        roles_bands=_roles(), fc_hz=FC_HZ, acknowledgement_binding=_BINDING,
        retries_per_pose=0, **kwargs,
    )
    wire = spec.to_dict()
    if with_calibration:
        assert wire["default_setup"]["calibration"]["calibration_id"] == record.calibration_id
        assert wire["default_setup"]["calibration"]["mode"] == "serial"
    else:
        assert "default_setup" not in wire


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
    analyze = v2evidence.bind_production_analyze(meta=meta)
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
    with caplog.at_level(_logging.WARNING, logger="jasper.web.correction_crossover_v2_evidence"):
        out = analyze(
            program, result, MeasurementPriors(crossover_fc_hz=FC_HZ),
            MeasurementGeometry(),
            phase="verify",
        )

    assert out == "analysis"
    assert seen["calibration"] is not None
    assert meta["calibration"]["verify"] == {
        "applied": True, "calibration_id": record.calibration_id,
        "curve_fingerprint": json_fingerprint(record.curve.to_dict()),
    }
    assert not event_records(caplog, "correction.crossover_v2_uncalibrated_capture")


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
    analyze = v2evidence.bind_production_analyze(meta=meta)
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
    assert meta["calibration"]["verify"] == {"applied": False, "calibration_id": None, "curve_fingerprint": None}
    assert event_records(caplog, "correction.crossover_v2_uncalibrated_capture")
    assert event_records(caplog, "correction.calibration_device_identity_mismatch")

    # The household record was never re-persisted against the wrong device.
    from jasper.audio_measurement.household_mic import (
        household_mic_path,
        read_household_mic,
    )

    saved = read_household_mic(path=household_mic_path())
    assert saved is not None
    assert saved.model_key == "minidsp_umik2"


def test_status_block_reports_needs_recovery_and_phase():
    class _NeedsRecovery:
        needs_recovery = True

    v2volume.set_volume_plan_for_tests(_NeedsRecovery())
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK],
        "applied": False,
    })
    block = v2status.crossover_v2_status_block()
    assert block["needs_recovery"] is True
    assert block["phase"] == PHASE_MEASURE
    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_REVIEW


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
    return v2durable.candidate_summary(
        MeasuredCrossoverCandidate(
            program_id="prog-abc",
            analysis=analysis
            or {
                "alignment_confidence": 0.9,
                "predicted_ripple_db": 1.1,
                "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4},
            },
            source_preset=_preset(),
            role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
            **extra,
        )
    )


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
        # exists at /sound/speaker/) from a real class's own prior (there is none).
        pytest.param(
            {
                "woofer": {
                    "role": "woofer",
                    "observe_octave_summary": {"8000": -0.3},
                    "reason_summary": {
                        "8000": "envelope_limited_by_mic_tier",
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
    """Persist whether the operator pinned polarity, including pre-field candidates."""
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
    assert v2durable.candidate_summary(None) is None


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
    v2volume.set_volume_plan_for_tests(plan)

    # Within the ceiling: cheap no-op, nothing drained.
    assert v2volume.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is False
    assert plan.measurement_volume_db == -20.0

    # Past the ceiling: force-drained back to the household volume.
    clock[0] = 2000.0
    assert v2volume.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True
    assert plan.measurement_volume_db is None
    assert cam.vol == -15.0


def test_a_raising_ceiling_drain_still_reports_the_expired_ceiling(monkeypatch):
    """The drain RAISES, so there is no outcome at all; the gate still hears
    that the ceiling expired."""
    plan, cam, _claim, clock = _live_measurement_session(monkeypatch)
    clock[0] += 3600.0

    def _raise(*_a, **_kw):
        raise RuntimeError("camilla went away mid-drain")

    monkeypatch.setattr(plan, "enforce_ceiling", _raise)

    assert v2volume.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    ) is True


def test_recover_on_a_deferral_reports_no_recovery(monkeypatch):
    """Arm 3 of G2: a deferral is not a recovery, and must not be sold as one.

    ``succeeded`` gates the household's ``recovered`` banner, so counting
    DEFERRED as success told the household its volume was restored while a
    live session still held the fader.
    """
    _plan, cam, _claim, _clock = _live_measurement_session(monkeypatch)

    succeeded, recovery = v2volume.recover_session_volume(_bg_run_async, lambda: cam)

    assert succeeded is False, "a deferral was reported to the household as recovered"
    assert recovery == v2volume.RECOVERY_DEFERRED


def test_the_recovery_deferred_value_tracks_the_enum():
    """``RECOVERY_DEFERRED`` mirrors a value this module cannot import at
    runtime (the plan is a ``TYPE_CHECKING``-only import), so pin them equal."""
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    assert v2volume.RECOVERY_DEFERRED == SessionVolumeRestoreResult.DEFERRED.value


def test_v2_volume_recovery_active_tracks_needs_recovery():
    class _NeedsRecovery:
        needs_recovery = True

    v2volume.set_volume_plan_for_tests(_NeedsRecovery())
    assert v2volume.v2_volume_recovery_active() is True

    class _Clean:
        needs_recovery = False

    v2volume.set_volume_plan_for_tests(_Clean())
    assert v2volume.v2_volume_recovery_active() is False


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

    v2volume.set_volume_plan_for_tests(_Plan())
    cam = _FakeVolCam(-20.0)
    _own_the_fader(monkeypatch, cam)
    succeeded, recovery = v2volume.recover_session_volume(_bg_run_async, lambda: cam)
    assert succeeded is True
    assert recovery == "exact_restored"
    assert drained == [True]
    assert cam.vol == -15.0


def test_web_binding_carries_declared_protection_and_the_same_graph(monkeypatch, tmp_path):
    from jasper.active_speaker.crossover_v2 import composition, door
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR

    bound = {}
    graph = SimpleNamespace(installed_graph_yaml=lambda: "graph", level_reference_yaml="graph")
    def bind_graph(profile, **kwargs):
        bound["profile"], bound["graph_dir"] = profile, kwargs["config_dir"]
        return graph
    def bind_compose(**kwargs):
        bound["composer"] = kwargs
        return "composer"
    monkeypatch.setattr(door, "bind_measurement_graph", bind_graph)
    monkeypatch.setattr(composition, "bind_program_composer", bind_compose)
    protection = {"woofer": (), "tweeter": ()}
    play = v2evidence.bind_production_play(
        program_for_phase=lambda phase: phase, camilla_factory=lambda: None,
        evidence_store=SimpleNamespace(bundle_dir=tmp_path), capture_session_id="capture",
        topology=None, preset=None, role_channels={"woofer": 0, "tweeter": 1},
        playback_device="null", safety_profile={}, role_targets={},
        session_volume_db=-20, protection_sections_by_role=protection, roles=(),
    )
    assert play.graph is graph and play.compose == "composer"
    assert bound["profile"].protection_sections_by_role is protection
    assert bound["graph_dir"] == bound["composer"]["config_dir"] == str(DEFAULT_CAMILLA_CONFIG_DIR)
    assert bound["composer"]["graph_yaml"]() == "graph"


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
    """Seed the declaration and evidence used by the real apply loaders."""
    monkeypatch.setattr(v2state, "_state_path_override", tmp_path / "v2_state.json")
    monkeypatch.setattr(v2host, "resolve_conductor_context", lambda status: object())
    from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview

    from tests.test_active_speaker_baseline_profile import _draft, _dual_apple_topology

    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions")
    topology = _dual_apple_topology()
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    save_output_topology(topology, topology_path)

    from jasper.active_speaker.design_draft import build_design_draft
    from tests.test_active_speaker_driver_safety import _manual_settings

    manual = _manual_settings()
    tweeter = manual["drivers"][1]
    tweeter["hard_excitation_band_hz"][0] = 2000
    tweeter["measurement_band_hz"][0] = 2000
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 2000
    manual["drivers"][0]["required_protection_filters"] = []
    draft = build_design_draft(topology, driver_research=_draft(topology)["driver_research"], manual_settings=manual,
                               created_at="2026-06-14T12:00:00Z")
    draft_path = tmp_path / "design_draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(draft_path))

    preview = ensure_crossover_preview_ready()

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
        for name, spec in (config.get("filters") or {}).items()
        if str((spec.get("parameters") or {}).get("type", "")).startswith("LinkwitzRiley")
        and "_declared_protection_" not in name
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
        manual_settings={**json.loads((tmp_path / "design_draft.json").read_text())["manual_settings"], "crossover_candidates": [manual_candidate]},
        created_at="2026-08-09T12:00:00Z",
    )
    draft["revision"] = 1
    (tmp_path / "design_draft.json").write_text(
        json.dumps(draft), encoding="utf-8",
    )
    preview = ensure_crossover_preview_ready()
    from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview

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
    v2state.save_v2_state({
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


def test_alternative_apply_loads_exact_candidate_then_records_sound(
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
    assert load_design_draft()["manual_settings"]["crossover_candidates"][0][
        "frequency_hz"
    ] == 2750.0
    # …and the speaker is PLAYING it. The declaration and the emitted graph
    # agreeing is the whole promise of deriving the write from the candidate.
    assert _emitted_crossover_filters(cam) == {
        "LinkwitzRileyLowpass": (2750.0, 4),
        "LinkwitzRileyHighpass": (2750.0, 4),
    }
    state = v2state.load_v2_state()
    assert state["accepted_sound_revision"] == 2
    assert state["applied"] is True


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
            refusal_copy.CrossoverV2Refused,
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
    assert excinfo.value.code == "crossover_below_declared_protection_floor"

    draft = load_design_draft()
    assert draft["manual_settings"]["crossover_candidates"][0][
        "frequency_hz"
    ] == 2500
    # Not one write happened: the revision the seed left is still the live one,
    # so there is nothing for a household to undo and nothing for the next
    # measurement session to read as its configured crossover.
    assert draft["revision"] == 1
    state = v2state.load_v2_state() or {}
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
    from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    _seed_alternative_apply(monkeypatch, tmp_path)
    # Recompile the candidate from what /sound DECLARES, so this apply asks for
    # no declaration change at all.
    preview = ensure_crossover_preview_ready()
    configured_preset, issues, _gates = compile_preset_from_crossover_preview(
        load_output_topology(), preview,
    )
    assert configured_preset is not None, issues
    as_declared = _run6_measured_candidate(configured_preset)
    v2state.save_v2_state({
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
    state = v2state.load_v2_state() or {}
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
    v2volume.set_volume_plan_for_tests(plan)
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

    v2state.save_v2_state({
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
    assert v2state.load_v2_state()["expected_post_apply_offset_db"] == _APPLY_OFFSET_DB
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

    v2state.save_v2_state({
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
    ensure_crossover_preview_ready()

    with pytest.raises(refusal_copy.CrossoverV2Refused) as refused:
        _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()},
               _bg_run_async, _FakeApplyAndVolumeCam)
    assert refused.value.code == "tweeter:required_highpass_missing"
    assert "expected_post_apply_offset_db" not in v2state.load_v2_state()
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
    v2state.save_v2_state({
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
    offset = v2state.load_v2_state()["expected_post_apply_offset_db"]
    assert offset == _APPLY_OFFSET_DB

    # One more capture in the SAME session, then the re-arm's brand-new one.
    for session_id in ("cap_run6", "cap_rearm"):
        v2state.persist_conductor_state(
            _StubConductor(session_id), failure_code=None,
        )
        assert v2state.load_v2_state()["expected_post_apply_offset_db"] == offset, session_id


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
            applied=self._applied, gain_plan_db=None,
            candidate_fingerprint=None, cloud_close="",
        )


def test_only_verify_rebind_carries_an_accepted_sound_revision():
    v2state.save_v2_state({"session_id": "old", "accepted_sound_revision": 4})
    v2state.persist_conductor_state(_StubConductor("verify"), failure_code=None)
    assert (v2state.load_v2_state() or {})["accepted_sound_revision"] == 4

    v2state.persist_conductor_state(
        _StubConductor("measure", session_phases=(PHASE_CHECK, PHASE_MEASURE)),
        failure_code=None,
    )
    assert (v2state.load_v2_state() or {})["accepted_sound_revision"] is None


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

    The re-arm's brand-new session id is the hard case, and the one all three
    bugs hit, so that is what this crosses.
    """
    # (1) What a persist can rebuild from the conductor alone, with an empty
    # prior so nothing can be carried forward.
    v2state.save_v2_state({"session_id": "s1"})
    v2state.persist_conductor_state(_StubConductor("s1"), failure_code=None)
    from_conductor_alone = {
        key for key, value in (v2state.load_v2_state() or {}).items()
        if value is not None
    }

    # (2) What the apply path establishes on top of it.
    v2state.save_v2_state({
        "session_id": "s1", "applied": False,
        "accepted_phases": [PHASE_MEASURE], "candidate": {"fingerprint": "fp"},
    })
    v2state.observe_apply_success(
        "fp",
        previous_candidate_fingerprint="fp-prior-measured",
        expected_post_apply_offset_db=-22.458,
    )
    after_apply = dict(v2state.load_v2_state() or {})
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
    v2state.persist_conductor_state(_StubConductor("cap_rearm"), failure_code=None)
    after_persist = v2state.load_v2_state() or {}
    for key in sorted(host_owned):
        assert after_persist.get(key) == after_apply[key], (
            f"{key!r} is written by the apply path and erased by "
            "persist_conductor_state — add a carry-forward line for it"
        )


def test_a_pre_pr6b_candidate_payload_still_applies(monkeypatch, tmp_path):
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)

    pre_pr6b_payload = dict(candidate.to_dict())
    assert "exclusion_evidence" in pre_pr6b_payload
    del pre_pr6b_payload["exclusion_evidence"]  # the pre-PR-6b persisted shape

    v2state.save_v2_state({
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
    assert v2state.load_v2_state()["applied"] is True


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
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Session,
        V2FlowSeams,
        V2RecordPublishers,
    )

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
                analyze=lambda *a, **k: None,
                records=V2RecordPublishers(check=lambda *a, **k: None),
            ),
            driver_spacing_m=0.15,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            index_phase_map={1: PHASE_VERIFY},
        )
        v2state.persist_conductor_state(conductor, failure_code=None)

    # --- run 1: a v2-written apply, no pre-existing profile to restore to ---
    run1_candidate = _prior_measured_candidate(preset)
    v2state.save_v2_state({
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
    assert v2state.load_v2_state()["previous_candidate_fingerprint"] is None

    # The deferred VERIFY always auto-arms right after an apply — reproduce
    # its rebind-and-persist before the household ever reaches run 2.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run1")
    assert v2state.load_v2_state()["applied"] is True
    assert v2state.load_v2_state()["previous_candidate_fingerprint"] is None

    # --- run 2 over run 1: also v2-written, through the SAME production seam ---
    run2_candidate = _run6_measured_candidate(preset)
    v2state.save_v2_state({
        **v2state.load_v2_state(),
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

    state_after_run2_apply = v2state.load_v2_state()
    assert (
        state_after_run2_apply.get("previous_candidate_fingerprint")
        == run1_candidate.fingerprint
    )

    # The P0 assertion: run 2's own deferred VERIFY rebind must NOT wipe the
    # pointer — this is exactly where the stash went null before the fix.
    _simulate_deferred_verify_rearm(verify_session_id="verify_of_run2")
    state_after_verify_rearm = v2state.load_v2_state()
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
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    prior_candidate = _prior_measured_candidate(preset)
    prior_cam = _FakeApplyCam()
    prior_payload = _apply({"expected_candidate_fingerprint": prior_candidate.fingerprint, "candidate": prior_candidate.to_dict()},
                           _bg_run_async, lambda: prior_cam)
    assert prior_payload["status"] == "applied", prior_payload.get("issues")

    run8_candidate = _run6_measured_candidate(preset)
    v2state.save_v2_state({
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
    v2state.reset_v2_journey_state()

    state = v2state.load_v2_state()
    assert state is not None
    assert state["applied"] is True
    assert state["previous_candidate_fingerprint"] == prior_candidate.fingerprint
    assert state["accepted_phases"] == []
    assert state["candidate"] is None
    # The envelope serves the clean start screen…
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_CHECK


def test_v2_session_start_ensures_preview_and_survives_start_over_then_reapply(
    monkeypatch, tmp_path,
):
    from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview
    from jasper.web import correction_crossover_flow as reset_flow

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)

    candidate = _run6_measured_candidate(preset)
    v2state.save_v2_state({
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

    # Start-over — the REAL handle_reset (real reset_measurement_journey;
    # only the envelope-rendering tail is stubbed, mirroring
    # test_correction_crossover_reset.py's real-clear pattern). The other
    # measurement-journey artifacts route to tmp_path too so the real clear
    # never touches /var/lib/jasper.
    for env_name in (
        "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH",
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE",
    ):
        monkeypatch.setenv(env_name, str(tmp_path / f"{env_name.lower()}.json"))
    monkeypatch.setattr(reset_flow, "handle_status", lambda *, capture=None: ({}, 200))
    monkeypatch.setattr(
        "jasper.web.correction_crossover_flow._build_envelope_logged",
        lambda status: {"screen": "start", "active": True, "steps": [], "nudges": []},
    )

    _reset_payload, reset_status = reset_flow.handle_reset()

    assert reset_status == 200
    reensured = ensure_crossover_preview_ready()
    assert reensured["status"] == "ready_for_protected_staging"

    preset_again, issues, _gates = compile_preset_from_crossover_preview(
        topology, reensured,
    )
    assert preset_again is not None, issues
    candidate_again = _run6_measured_candidate(preset_again)
    v2state.save_v2_state({
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
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE",
        str(tmp_path / "design_draft_never_saved.json"),
    )

    with pytest.raises(refusal_copy.CrossoverV2Refused, match="not ready for measurement"):
        ensure_crossover_preview_ready()


class _RecordingEvidenceStore:
    """Minimal stand-in for the commissioning evidence store."""

    session_id = "bundle-session"

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish_json_artifact(self, relpath, payload):
        self.published.append((relpath, payload))
        return SimpleNamespace(fingerprint="fp-check")

    def identify_artifact(self, relpath):
        return SimpleNamespace(relative_path=relpath, fingerprint="fp-check")


def test_check_evidence_artifact_carries_the_per_role_level_solve():
    """`check.json` is the session's durable record of what MEASURE was about
    to be played at. The solved gains alone are not self-explaining, so the
    artifact carries the derivation beside them — which limit chose each
    driver's level, and the ambient band it was solved against."""
    from jasper.audio_measurement.program_analysis import GainPlan, RoleGainSolve

    store = _RecordingEvidenceStore()
    publish_check, refs = v2evidence.bind_evidence_publishers(
        store, "capture-session", asyncio.run
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
    publish_check, _refs = v2evidence.bind_evidence_publishers(
        store, "capture-session", asyncio.run
    )
    publish_check(
        GainPlan(
            gain_db={"woofer": -11.0}, predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
        {"bands": []},
    )
    (_relpath, payload), = store.published
    assert payload["role_solves"] == {}


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
    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    prior_candidate = _prior_measured_candidate(preset)
    _bank_candidate(monkeypatch, tmp_path, prior_candidate)
    prior_cam = _FakeApplyCam()
    prior_payload = _apply({"expected_candidate_fingerprint": prior_candidate.fingerprint},
                           _bg_run_async, lambda: prior_cam)
    assert prior_payload["status"] == "applied", prior_payload.get("issues")

    candidate = _run6_measured_candidate(preset)
    v2state.save_v2_state({
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

    pointer = (v2state.load_v2_state() or {}).get("previous_candidate_fingerprint")
    assert pointer, "the apply must record the displaced measured candidate"
    return pointer


@pytest.mark.parametrize("record", ["absent", "applied", "legacy", "pruned"])
@pytest.mark.parametrize("campaigns_exist", [False, True])
def test_status_never_discovers_candidates_on_a_cold_or_empty_box(monkeypatch, tmp_path, record, campaigns_exist):
    from jasper.web.correction_crossover_flow import handle_status

    if record != "absent":
        _apply_prior_then_v2_candidate(monkeypatch, tmp_path)
        path = tmp_path / "baseline_profile.json"
        applied = json.loads(path.read_text())
        if record == "legacy":
            applied.pop("candidate_artifact_path")
            path.write_text(json.dumps(applied))
        elif record == "pruned":
            Path(applied["candidate_artifact_path"]).unlink()
    else:
        _seed_baseline_apply_environment(monkeypatch, tmp_path)
    campaigns = tmp_path / "campaigns"
    if campaigns_exist:
        campaigns.mkdir(exist_ok=True)

    def bank_walk(*args, **kwargs):
        raise AssertionError

    monkeypatch.setattr("jasper.active_speaker.candidate_bank.find_banked_candidate", bank_walk)
    monkeypatch.setattr("jasper.active_speaker.candidate_bank._iter_candidate_paths", bank_walk)
    payload, code = handle_status()
    assert code == 200
    assert payload["setup"]["protected_profile"]["available"] is (record != "absent")


def test_apply_refuses_a_banked_candidate_after_pruning(monkeypatch, tmp_path):
    from jasper.active_speaker.candidate_bank import find_banked_candidate

    offered = _apply_prior_then_v2_candidate(monkeypatch, tmp_path)
    find_banked_candidate(offered).path.unlink()
    with pytest.raises(refusal_copy.CrossoverV2Refused) as exc:
        _apply({"expected_candidate_fingerprint": offered}, _bg_run_async, _FakeApplyCam)
    assert exc.value.code == "not_found"


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

    drained = v2volume.enforce_session_volume_ceiling_if_stale(
        _bg_run_async, lambda: cam
    )

    assert drained is True, "the ceiling still reports that it expired"
    assert cam.vol == writes_before, "the drain moved a fader it does not own"
    assert plan.needs_recovery is False, "a live session is not a recovery case"
    assert plan.unresolved_volume_safety is None, "nothing latched"
    v2volume.set_volume_plan_for_tests(None)


@pytest.mark.parametrize("route,handler_name", [
    ("session", "capture"), ("apply", "apply"),
    ("position-ready", "position_ready"), ("complete", "complete"),
    ("retake", "retake"),
])
def test_graph_refusal_reaches_the_http_client_with_its_code_and_action(
    monkeypatch, route, handler_name,
):
    from jasper.active_speaker.measurement_emit import MeasurementGraphRefused
    from jasper.web import correction_setup, correction_handlers

    def refuse(*args, **kwargs):
        raise MeasurementGraphRefused("measurement_candidate_required", "candidate-1")

    monkeypatch.setattr(correction_handlers, "_handle_crossover_v2_" + handler_name, refuse)
    handler_cls = correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=contextlib.nullcontext,
    )
    handler = handler_cls.__new__(handler_cls)
    handler.path = "/crossover/v2/" + route
    responses = []
    handler._send_json = lambda payload, status=200: responses.append((int(status), payload))
    correction_setup._dispatch_crossover(handler)
    status, body = responses.pop()
    assert 400 <= status < 500
    assert set(body) == {"ok", "code", "next_action", "error"}
    assert body["ok"] is False
    assert body["code"] == "measurement_candidate_required"
    assert isinstance(body["next_action"], dict)
    assert body["next_action"]["id"] == "select_candidate"


def _inline_body():
    from jasper.active_speaker.angle_capture import summed_at
    return {"plan": summed_at([0, 20]).to_dict()}


def test_session_duplicate_levels_returns_shared_bad_request(monkeypatch):
    body = _inline_body()
    body["plan"]["levels"] = [-10, -10]
    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _: body)
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", lambda: {})
    replies = []
    handler = SimpleNamespace(path="/crossover/v2/session", idle_hold=contextlib.nullcontext,
                              _send_json=lambda payload, status=200: replies.append((int(status), payload)))
    correction_setup._dispatch_crossover(handler)
    status, reply = replies.pop()
    assert status == 400 and reply["ok"] is False
    assert set(reply) == {"ok", "code", "next_action", "error"}
    assert reply["code"] == "walk_level_policy_invalid"
    assert isinstance(reply["next_action"], dict) and isinstance(reply["error"], str)


def _ready_inline(monkeypatch):
    from jasper.active_speaker import preflight_live
    from tests.test_preflight import ready_facts
    monkeypatch.setattr(preflight_live, "read_preflight_facts", lambda plan, **kwargs: ready_facts(
        plan, declared_target_ids=tuple(kwargs["context"].role_targets), roles_bands=kwargs["context"].roles_bands))


def _inline_context() -> V2ConductorContext:
    return V2ConductorContext(
        preset=_preset(), fc_hz=FC_HZ, roles_bands=tuple(_roles()),
        safety_profile={"targets": [{
            "role": role, "target_fingerprint": f"fp-{role}",
            "hard_excitation_band_hz": [20, 4000] if role == "woofer" else [300, 20000],
            "level_duration_limits": {"max_sweep_duration_s": 6.0,
                                      "max_effective_peak_dbfs": CAPS[role]},
            "required_protection_filters": [{
                "kind": kind, "cutoff_hz": cutoff,
                "minimum_slope_db_per_octave": 24.0,
            }],
        } for role, kind, cutoff in (
            ("woofer", "lowpass", 6000.0), ("tweeter", "highpass", 300.0),
        )]},
        role_targets={role: f"fp-{role}" for role in CAPS},
        driver_caps_dbfs=dict(CAPS),
        driver_sweep_duration_limits_s={role: 6.0 for role in CAPS},
        session_volume_db=SESSION_VOLUME_DB,
        driver_spacing_m=0.0, driver_spacing_source="unknown",
        topology=SimpleNamespace(topology_id="t-inline"),
        playback_device="hw:Test", role_channels={"woofer": 0, "tweeter": 1},
        sound_design_revision=1,
    )


def _inline_prepared(monkeypatch, tmp_path, body=None):
    _ready_inline(monkeypatch)
    monkeypatch.setattr(v2host, "resolve_conductor_context", lambda _: _inline_context())
    v2volume.set_volume_plan_for_tests(SimpleNamespace(needs_recovery=False))
    store = _bundle_store(tmp_path)
    monkeypatch.setattr(v2evidence, "open_v2_evidence_store", lambda _: (store, store.session_id))
    return v2host.prepare_v2_session(body or _inline_body(), status={}, run_async=_bg_run_async, camilla_factory=None), store


def _store_seat_reference(reference):
    if reference is None:
        return
    from jasper.active_speaker.seat_level_reference import SeatLevelTarget, write_seat_level_reference
    write_seat_level_reference(
        reference_volume_db=reference, measured_db_spl=75.0,
        target=SeatLevelTarget(75.0, 1.0),
        sensitivity={"serial": "1234", "sens_factor_db": -12.0},
        max_main_volume_db=0.0,
    )


def test_a_branch_pair_this_box_never_declared_refuses_by_name(monkeypatch, tmp_path):
    """The one place that holds both the plan and the box. A front/rear take on a
    speaker with no rear output is refused before the microphone is placed;
    admission stays the independent tripwire behind it."""
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop
    from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
    from jasper.active_speaker.measurement_programs import BRANCH_PAIR_FRONT_REAR, REGIME_BRANCHES

    plan = AngleCaptureRequest((AngleStop(0, REGIME_BRANCHES, branch_pair=BRANCH_PAIR_FRONT_REAR),))
    assert "woofer:rear" not in _inline_context().role_targets
    with pytest.raises(refusal_copy.CrossoverV2Refused) as exc:
        _inline_prepared(monkeypatch, tmp_path, {"plan": plan.to_dict()})
    assert exc.value.code == "walk_branch_pair_undeclared"
    assert refusal_copy_for(exc.value.code)[1]


@pytest.mark.parametrize("prior_capture", [None, {"status": "complete", "kind": "crossover_v2:session"}])
@pytest.mark.parametrize(("reference", "level_source"), [(-18.0, "seat_reference"), (None, "program_default")])
def test_inline_session_creation_persists_the_plan_and_holds_nothing(
    monkeypatch, tmp_path, prior_capture, reference, level_source,
):
    from jasper.web import correction_capture

    monkeypatch.setattr(correction_capture, "_capture_slot", prior_capture)
    monkeypatch.setattr(correction_capture, "_pending_capture", None)
    monkeypatch.setattr(v2host, "_resolve_prepare_wired_mic", lambda: pytest.fail("live mic admission before join"))
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.door._measurement_claim", lambda: pytest.fail("claim before join"))
    _store_seat_reference(reference)
    before = v2state.load_v2_state()
    prepared, store = _inline_prepared(monkeypatch, tmp_path)
    kind = correction_capture.CaptureKind(
        label=prepared.label, open=prepared.open, run_and_consume=prepared.run_and_consume,
        position_gate=prepared.position_gate, session_id=prepared.session_id,
        join_entry=prepared.join_spec.capture_plan.entries[0],
    )
    result = correction_capture._stage_capture(kind, idle_hold=lambda _: pytest.fail("idle hold before join"))
    assert result["url"] == "/sound/speaker/crossover/"
    assert result["session_id"] == prepared.session_id
    staged = correction_capture._get_capture_slot_for("crossover_v2:")
    assert staged["status"] == "awaiting_join"
    assert correction_capture._get_capture_slot() is None
    published = prepared.position_gate.published()
    assert {key: published[key] for key in ("pending", "current")} == {"pending": None, "current": None}
    assert staged["run"] == published["run"]
    assert "pose" not in staged["run"]
    assert staged["run"]["poses"] == 2
    assert staged["run"]["sweeps_per_pose"] == [6, 3]
    assert staged["run"]["sweeps"] == 9
    env = v2projection.build_crossover_envelope_v2({
        "active": True, "setup": {"active": True, "status": "ready"}, "capture": staged,
    })
    assert env["round_lines"]
    assert env["capture"]["join"] == result["join"]
    plan = store.reopen_json_artifact(store.identify_artifact(f"evidence/v1/artifacts/crossover_v2/{prepared.session_id}/plan.json"))
    assert plan["stops"] == _inline_body()["plan"]["stops"]
    assert plan["level"]["level_db"] == reference
    assert plan["level_source"] == level_source
    assert v2state.load_v2_state() == before


@pytest.mark.parametrize("levels,phases", [
    (None, ("entry_baseline", "lateral", "lateral")),
    ((-18, -23), ("lateral",)),
    ((-8, -18), ("lateral",)),
])
def test_inline_preparation_binds_the_real_engine_without_fitting(
    monkeypatch, tmp_path, levels, phases
):
    from jasper.web import correction_crossover_v2_wired as wired
    from tests.test_correction_crossover_v2_wired import _device
    from tests.test_preflight import ready_facts
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, request_for_program
    from jasper.active_speaker.measurement_programs import program

    selected = program("bass")
    body = {"plan": request_for_program(selected, mover=selected.mover or "human", levels=levels).to_dict()} if levels else _inline_body()
    prepared, store = _inline_prepared(monkeypatch, tmp_path, body)
    _own_the_fader(monkeypatch, _FakeVolCam(-30))
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    v2volume.set_volume_plan_for_tests(SessionVolumePlan())
    monkeypatch.setattr(wired, "resolve_v2_wired_mic", _device)
    monkeypatch.setattr("jasper.audio_measurement.household_mic.resolved_household_sensitivity",
                        lambda _: ready_facts(AngleCaptureRequest.from_mapping(_inline_body()["plan"])).anchor.sensitivity)
    bound = {}

    def build(conductor, **kwargs):
        bound.update(conductor=conductor, **kwargs)
        return None

    monkeypatch.setattr(v2host, "_build_wired_run", build)
    opened = prepared.open()
    assert opened.pi_session.session_id == prepared.session_id
    assert not bound["door"].is_open
    assert bound["request"] == AngleCaptureRequest.from_mapping(store.reopen_json_artifact(
        store.identify_artifact(f"evidence/v1/artifacts/crossover_v2/{prepared.session_id}/plan.json")))
    assert prepared.join_spec.capture_plan.capture_target == len(bound["captures"])
    assert tuple(capture.spec.program_phase for capture in bound["captures"]) == phases
    assert (bound["execute"] is not None) == (bound["request"].levels is not None)


def test_pending_plan_keeps_the_active_captures_status_and_signals(monkeypatch):
    from jasper.web import correction_capture as capture
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    from jasper.platform.systemd import no_hold

    monkeypatch.setattr(capture, "_capture_slot", None)
    monkeypatch.setattr(capture, "_pending_capture", None)
    stopped = []
    assert capture._begin_capture_slot("crossover_v2:session", request_stop=lambda reason: stopped.append(True))
    kind = capture.CaptureKind("crossover_v2:session", lambda: None, lambda _: None,
        position_gate=PositionGate(), session_id="pending", join_entry=SimpleNamespace(screen={"position_deg": "0"}))
    capture._stage_capture(kind, idle_hold=no_hold)
    assert capture._get_capture_slot_for("crossover_v2:")["status"] == "starting"
    assert capture._join_capture(2, 1) is None
    assert capture._request_capture_stop("crossover_v2:")["status"] == "stopping"
    assert stopped == [True]
    capture._set_capture_slot({"kind": kind.label, "status": "complete"})
    assert capture._get_capture_slot_for("crossover_v2:")["session_id"] == "pending"
    assert capture._request_capture_stop("crossover_v2:")["status"] == "stopped"
    assert capture._pending_capture is None


@pytest.mark.parametrize("tier", ["full", "express", "remote", "unrecognised"])
def test_old_tier_is_read_as_unknown_and_omitted(tmp_path, tier):
    v2state.set_state_path_for_tests(tmp_path / "state.json")
    v2state.save_v2_state({"tier": tier, "session_id": "historic"})
    state = v2state.load_v2_state()
    assert state["session_id"] == "historic"
    assert "tier" not in state


def test_staging_a_second_plan_preserves_the_first_and_refuses_by_code(monkeypatch):
    from dataclasses import replace
    from jasper.web import correction_capture as capture
    from jasper.platform.systemd import no_hold

    monkeypatch.setattr(capture, "_capture_slot", None)
    monkeypatch.setattr(capture, "_pending_capture", None)
    kind = capture.CaptureKind("crossover_v2:session", lambda: None, lambda _: None,
                              session_id="first", join_entry=SimpleNamespace(screen={}))
    first = capture._stage_capture(kind, idle_hold=no_hold)
    with pytest.raises(refusal_copy.CrossoverV2Refused) as refused:
        capture._stage_capture(replace(kind, session_id="second"), idle_hold=no_hold)
    assert refused.value.code == "capture_slot_busy"
    assert capture._get_capture_slot_for("crossover_v2:") == first


@pytest.mark.parametrize("run_id", [None, "same"])
def test_concurrent_same_pose_joins_replay_the_accepted_payload(monkeypatch, run_id):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    from jasper.web import correction_capture as capture, correction_handlers as handlers
    from tests.test_correction_crossover_v2_wired import _fake_handler

    monkeypatch.setattr(capture, "_capture_slot", None)
    monkeypatch.setattr(capture, "_pending_capture", None)
    entered, release, second, drained = (threading.Event() for _ in range(4))
    opens = []
    def opened():
        opens.append(True)
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(pi_session=None)
    async def run(_):
        pass
    @contextlib.contextmanager
    def idle_hold(_):
        try:
            yield
        finally:
            drained.set()
    gate = PositionGate()
    kind = capture.CaptureKind("crossover_v2:session", opened, run, position_gate=gate,
                              session_id="same", join_entry=SimpleNamespace(screen={"position_deg": "0"}))
    capture._stage_capture(kind, idle_hold=idle_hold)
    def join(mark=None):
        if mark:
            mark.set()
        payload = {"index": 1, "attempt": 1, **({"run_id": run_id} if run_id else {})}
        return handlers._handle_crossover_v2_position_ready(_fake_handler(json.dumps(payload).encode()))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(join)
            assert entered.wait(2)
            other = pool.submit(join, second)
            assert second.wait(2)
            release.set()
            assert first.result(timeout=2) == other.result(timeout=2) == {
                "ok": True, "capture": {"status": "awaiting_capture"}}
        assert drained.wait(2)
        assert opens == [True]
        assert gate.join(kind.join_entry)["index"] == 1
        for payload in ({"index": 9, "attempt": 1}, {"index": 1, "attempt": 1, "run_id": "old"}):
            with pytest.raises(refusal_copy.CrossoverV2Refused) as refused:
                handlers._handle_crossover_v2_position_ready(_fake_handler(json.dumps(payload).encode()))
            assert refused.value.code == "capture_slot_busy"
    finally:
        release.set()


@pytest.mark.parametrize("applied,epoch,receipt,expected", [
    (True, 0, {"round_ordinal": 2}, 1),
    (False, 0, None, 0),
    (False, 2, None, 2),
    (True, 2, None, 2),
    (True, 2, {"round_ordinal": 3}, 3),
])
def test_start_over_carries_the_sequence_epoch(applied, epoch, receipt, expected, caplog):
    from jasper.active_speaker.crossover_v2.coordinator import series_position_from_state
    from tests._log_events import event_records, parse_event

    v2state.save_v2_state({"applied": applied, "round_ordinal_epoch": epoch, "round_receipt": receipt})
    with caplog.at_level(logging.INFO):
        v2state.reset_v2_journey_state()
    state = v2state.load_v2_state()
    position = series_position_from_state(state)
    assert (position.ordinal, position.ordinal_epoch) == (1, expected)
    assert (state or {}).get("round_receipt") is None
    events = event_records(caplog, "correction.crossover_v2_journey_reset_advanced_epoch")
    if applied and receipt:
        event = parse_event(events[0].getMessage())[1]
        assert event["reset_round_ordinal_from"] == str(receipt["round_ordinal"])
    else:
        assert not events


def test_restore_uses_the_saved_sound_inverse_and_the_previous_trial(monkeypatch, tmp_path):
    from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    selected = _seed_alternative_apply(monkeypatch, tmp_path)
    preset, _, _ = compile_preset_from_crossover_preview(load_output_topology(), build_crossover_preview(load_design_draft()))
    previous = _run6_measured_candidate(preset)
    cam = _FakeApplyCam()
    applied = _apply({"candidate": previous.to_dict(), "expected_candidate_fingerprint": previous.fingerprint},
                     _bg_run_async, lambda: cam)
    assert applied["status"] == "applied"
    state = v2state.load_v2_state()
    state.update(candidate={"fingerprint": selected.fingerprint}, applied=False)
    v2state.save_v2_state(state)
    applied = _apply({"candidate": selected.to_dict(), "expected_candidate_fingerprint": selected.fingerprint},
                     _bg_run_async, lambda: cam)
    assert applied["status"] == "applied"
    for path in tmp_path.rglob("run_manifest.json"):
        path.unlink()
    v2state.reset_v2_journey_state()
    restored = v2apply.handle_v2_apply({"expected_candidate_fingerprint": previous.fingerprint},
                                     _bg_run_async, lambda: cam)
    assert restored["status"] == "applied"
    state = v2state.load_v2_state()
    assert state["accepted_sound_revision"] == 3
    assert state["accepted_sound_candidate_fingerprint"] == previous.fingerprint
    assert state["accepted_sound_declaration_change"]["previous_hz"] == 2750.


def test_a_measure_only_session_resolves_to_review_never_done():
    """**The work order's premise 6, and PR-T2's first pin.**

    ``crossover_v2_phase`` walks the recorded ``session_phases`` and returns
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

    v2state.save_v2_state({
        "session_id": "cap_x",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE, PHASE_CLOUD_MEASURE],
        "applied": False,
    })
    assert v2status.crossover_v2_status_block()["phase"] == PHASE_REVIEW


@pytest.mark.parametrize("layers", [(), ("room",), ("bass",), ("room", "bass")])
def test_apply_after_draft_edit_loads_the_trial_composers_exact_bytes(monkeypatch, tmp_path, layers):
    from jasper.sound.settings import saved_sound_layers
    from jasper.active_speaker.candidate_bank import publish_authored_candidate
    from jasper.active_speaker.measurement_emit import compile_tuning_graph

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    from tests.test_active_speaker_measured_crossover_candidate import _room_correction
    from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION

    candidate = replace(_run6_measured_candidate(preset), analysis={"measurement_status": "unmeasured"},
                        room_correction=_room_correction() if "room" in layers else {},
                        bass_extension=BASS_EXTENSION if "bass" in layers else {})
    publish_authored_candidate(candidate)
    path = tmp_path / "design_draft.json"
    draft = json.loads(path.read_text())
    draft.update(revision=7, updated_at="2026-09-13T12:00:00Z")
    draft["manual_settings"]["driver_spacing_mm"] = 190
    path.write_text(json.dumps(draft))
    declaration = v2apply.load_tuning_declaration(topology, design_draft=v2apply.load_design_draft(topology=topology))
    preference_filters, trim_db = saved_sound_layers()
    expected = compile_tuning_graph(declaration, candidate=candidate,
                                    preference_filters=preference_filters, output_trim_db=trim_db).encode("utf-8")
    cam = _FakeApplyCam()
    result = v2apply.handle_v2_apply({"expected_candidate_fingerprint": candidate.fingerprint}, _bg_run_async, lambda: cam)
    assert result["status"] == "applied"
    assert Path(cam.path).read_bytes() == expected
    record = json.loads((tmp_path / "baseline_profile.json").read_text())
    assert record["source"]["measured_candidate_fingerprint"] == candidate.fingerprint
    assert record["source"]["design_draft_updated_at"] == draft["updated_at"]
    assert record["config"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert record["apply"]["result"] == "success"
    assert not list(tmp_path.rglob("run_manifest.json"))


@pytest.mark.parametrize("clear_alignment", [False, True])
def test_document_apply_keeps_timing_only_when_alignment_is_inherited(monkeypatch, tmp_path, clear_alignment):
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
    from jasper.active_speaker.candidate_bank import BankedCandidate, publish_authored_candidate
    from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
    from jasper.active_speaker.crossover_v2.prescription_document import judge_prescription_document

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    incumbent = replace(_run6_measured_candidate(preset), analysis={"measurement_status": "unmeasured"})
    publish_authored_candidate(incumbent)
    cam = _FakeApplyCam()
    assert v2apply.handle_v2_apply(
        {"expected_candidate_fingerprint": incumbent.fingerprint}, _bg_run_async, lambda: cam,
    )["status"] == "applied"
    profile_path = tmp_path / "baseline_profile.json"
    profile = json.loads(profile_path.read_text())
    timing = {"delay_us": 22, "polarity": "normal", "provenance": "set_by_user"}
    profile["timing"] = timing
    profile_path.write_text(json.dumps(profile))
    base = candidate_from_applied_profile(topology, load_applied_baseline_profile_state())
    sections = {"driver": {}, **({"alignment": {}} if clear_alignment else {})}
    document = {"kind": "jts_prescription", "schema": 1, "base": "saved",
                "sections": sections, "rationale": "Reset driver tuning."}
    candidate = judge_prescription_document(
        document, base=BankedCandidate(base, "", "", profile_path),
    )
    publish_authored_candidate(candidate)

    assert v2apply.handle_v2_apply(
        {"expected_candidate_fingerprint": candidate.fingerprint}, _bg_run_async, lambda: cam,
    )["status"] == "applied"
    applied = load_applied_baseline_profile_state()
    assert applied is not None
    assert applied.get("timing") == (None if clear_alignment else timing)


@pytest.mark.parametrize("fault,code", [
    ("bank", "not_found"), ("declaration", "tweeter:required_highpass_missing"),
    ("floor", "crossover_below_declared_protection_floor"),
    ("graph", "baseline_graph_safety_proof_failed"),
    ("load", "apply_failed"), ("malformed", "driver_protection_invalid"),
    ("compose", "compose_refused"), ("live_floor", "crossover_below_declared_protection_floor"),
    ("identity", "measurement_candidate_speaker_mismatch"),
])
def test_apply_keeps_unsafe_config_refusals(monkeypatch, tmp_path, caplog, fault, code):
    caplog.set_level(logging.INFO, logger=v2apply.__name__)
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    if fault == "floor":
        candidate = replace(candidate, source_preset=replace(candidate.source_preset, crossover_regions=tuple(
            replace(region, fc_hz=500.) for region in candidate.source_preset.crossover_regions)))
    _bank_for_apply({"candidate": candidate.to_dict()})
    if fault in {"malformed", "compose"}:
        def refuse(*args, **kwargs):
            exc = ValueError("bad input") if fault == "malformed" else ActiveSpeakerConfigError("bad input")
            exc.code = None
            raise exc
        if fault == "malformed":
            monkeypatch.setattr("jasper.active_speaker.measurement_emit.confirmed_protection_sections", refuse)
        else:
            monkeypatch.setattr(v2apply, "compile_tuning_graph", refuse)
    if fault in {"live_floor", "identity"}:
        from jasper.active_speaker.design_draft import build_design_draft
        path = tmp_path / "design_draft.json"
        draft = json.loads(path.read_text())
        tweeter = draft["manual_settings"]["drivers"][1]
        if fault == "live_floor":
            tweeter["required_protection_filters"][0]["cutoff_hz"] = 3000.0
            tweeter["hard_excitation_band_hz"][0] = 3000.0
            tweeter["measurement_band_hz"][0] = 3000.0
            draft["manual_settings"]["crossover_candidates"][0]["frequency_hz"] = 3500.0
        else:
            tweeter["model"] = "different-driver"
        draft = build_design_draft(v2apply.load_output_topology(), driver_research=draft["driver_research"], manual_settings=draft["manual_settings"])
        path.write_text(json.dumps(draft))
    if fault == "declaration":
        path = tmp_path / "design_draft.json"
        draft = json.loads(path.read_text())
        draft["manual_settings"]["drivers"][1].pop("required_protection_filters")
        draft["manual_settings"]["drivers"][1].pop("recommended_highpass_hz", None)
        path.write_text(json.dumps(draft))
    elif fault == "graph":
        compile_graph = v2apply.compile_tuning_graph
        def unsafe(*args, **kwargs):
            return compile_graph(*args, **kwargs).replace("volume_limit: 0.0", "volume_limit: 1.0")
        monkeypatch.setattr(v2apply, "compile_tuning_graph", unsafe)
    before = (tmp_path / "design_draft.json").read_bytes()
    state_before = v2state.load_v2_state()
    cam = _FakeApplyCam()
    if fault == "load":
        async def fail(path, **kwargs):
            return False
        cam.set_config_file_path = fail
    raw = {"expected_candidate_fingerprint": "0" * 64 if fault == "bank" else candidate.fingerprint}
    if fault == "load":
        result = v2apply.handle_v2_apply(raw, _bg_run_async, lambda: cam)
        assert result["issue"]["code"] == code
        assert result["status"] == "apply_failed"
        assert result["apply"]["result"] == "load_failed"
        assert result["apply"]["rollback_attempted"] is False
    else:
        with pytest.raises(refusal_copy.CrossoverV2Refused) as refused:
            v2apply.handle_v2_apply(raw, _bg_run_async, lambda: cam)
        assert refused.value.code == code
        if fault == "graph":
            from jasper.active_speaker import runtime_contract
            from jasper.web.correction_runtime import refusal_envelope

            # The refusal names WHICH door refused: a bare code sent the
            # operator to read the graph by hand.
            envelope = refusal_envelope(refused.value)
            assert envelope["code"] == code
            assert envelope["error"] != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME
            assert [(issue["severity"], issue["code"]) for issue in envelope["issues"]] == [
                ("blocker", "volume_limit_positive")]
    fields = event_fields(caplog, "correction.crossover_v2_apply")
    assert fields["status"] == ("apply_failed" if fault == "load" else "blocked")
    assert fields["code"] == code
    assert cam.path is None
    assert (tmp_path / "design_draft.json").read_bytes() == before
    assert baseline_profile.load_applied_baseline_profile_state() is None
    assert v2state.load_v2_state() == state_before
    if fault == "load":
        failed = json.loads((tmp_path / "baseline_profile.json").read_text())
        assert failed["status"] == "apply_failed"
        assert failed["apply"] == result["apply"]
        assert [(issue["severity"], issue["code"]) for issue in failed["issues"]] == [
            ("blocker", "baseline_profile_apply_failed"),
        ]
        assert "applied_recomposition_profile" not in failed
    else:
        assert not (tmp_path / "baseline_profile.json").exists()


def test_apply_proves_the_snapshot_it_persists(monkeypatch, tmp_path):
    """The pre-apply proof reads the section set the applied record writes.

    Both snapshots come from one builder. Hand-assembling the proof's copy
    dropped ``rear_calibration`` and refused every banked cardioid candidate
    against a plain role chain (ADR-0322); no graph the route emits differs
    between the two, so the proof's own input is what this pins.
    """
    from jasper.active_speaker import baseline_profile, runtime_contract
    from jasper.active_speaker.candidate_bank import publish_authored_candidate

    _topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = replace(_run6_measured_candidate(preset), analysis={"measurement_status": "unmeasured"})
    publish_authored_candidate(candidate)
    classify, proved = runtime_contract.classify_bass_extension_graph, []

    def record(*args, **kwargs):
        snapshot = (kwargs.get("applied_baseline_state") or {}).get("recomposition_snapshot")
        if snapshot is not None:
            proved.append(dict(snapshot))
        return classify(*args, **kwargs)

    monkeypatch.setattr(runtime_contract, "classify_bass_extension_graph", record)
    assert v2apply.handle_v2_apply({"expected_candidate_fingerprint": candidate.fingerprint},
                                   _bg_run_async, lambda: _FakeApplyCam())["status"] == "applied"
    applied = baseline_profile.load_applied_baseline_profile_state()
    assert set(proved[0]) == set(applied["recomposition_snapshot"])


@pytest.mark.parametrize("measured", [True, False])
def test_apply_record_preserves_domain_and_measured_level_evidence(monkeypatch, tmp_path, measured):
    from jasper.active_speaker import baseline_profile, driver_base_trim
    from jasper.sound.settings import saved_sound_layers

    topology, preset = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    candidate = _run6_measured_candidate(preset)
    if not measured:
        candidate = replace(candidate, analysis={"measurement_status": "unmeasured"})
    cam = _FakeApplyCam()
    result = _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()}, _bg_run_async, lambda: cam)
    applied = baseline_profile.load_applied_baseline_profile_state()
    from jasper.active_speaker.measurement_emit import compile_tuning_graph, load_tuning_declaration
    from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
    assert applied["recomposition_snapshot"]["domain"] == "full"
    preference_filters, trim_db = saved_sound_layers()
    assert Path(applied["config"]["path"]).read_text() == compile_tuning_graph(
        load_tuning_declaration(topology), candidate=candidate_from_applied_profile(topology, applied),
        preference_filters=preference_filters, output_trim_db=trim_db)
    assert applied["level_match"]["applied"] is measured
    record = driver_base_trim.load_base_trim()
    if measured:
        assert record["trims_db"] == candidate.role_attenuations_db
        assert record["speaker_group_ids"] == applied["automatic_candidate"]["measured_group_ids"]
        assert set(applied["corrections_source"].values()) == {"measured"}
        assert set(applied["gain_provenance"].values()) == {"measured"}
        assert all(set(fields.values()) == {baseline_profile.PROVENANCE_MEASURED} for fields in applied["corrections_provenance"].values())
        assert record["measured_at"] == applied["level_match"]["newest_capture_at"]
    else:
        assert record is None
    assert result["status"] == "applied"


@pytest.mark.parametrize("phase", ["compose", "load", "record"])
def test_apply_does_not_turn_untyped_faults_into_refusals(monkeypatch, tmp_path, phase):
    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    _bank_for_apply({"candidate": candidate.to_dict()})
    failure = ValueError("unexpected fault")
    def fail(*args, **kwargs):
        raise failure
    @contextlib.asynccontextmanager
    async def failed_load(*args, **kwargs):
        raise failure
        yield
    if phase == "compose":
        monkeypatch.setattr(v2apply, "compile_tuning_graph", fail)
    elif phase == "load":
        monkeypatch.setattr(v2apply.baseline_apply, "load_composed_graph", failed_load)
    else:
        monkeypatch.setattr(v2state, "observe_apply_success", fail)
    with pytest.raises(ValueError) as caught:
        v2apply.handle_v2_apply({"expected_candidate_fingerprint": candidate.fingerprint}, _bg_run_async, _FakeApplyCam)
    assert caught.value is failure


def test_declaration_record_failure_does_not_hide_a_successful_apply(monkeypatch, tmp_path, caplog):
    candidate = _seed_alternative_apply(monkeypatch, tmp_path)
    def refuse(**kwargs):
        raise ValueError("declaration changed")
    monkeypatch.setattr(v2apply, "apply_measured_crossover_geometry", refuse)
    cam = _FakeApplyCam()
    result = _apply({"expected_candidate_fingerprint": candidate.fingerprint, "candidate": candidate.to_dict()}, _bg_run_async, lambda: cam)
    assert result["status"] == "applied"
    assert result["apply"]["active_config_path"] == cam.path
    assert result["declaration_update"]["status"] == "failed"
    assert result["declaration_update"]["code"] == "ValueError"
    assert event_records(caplog, "correction.crossover_v2_declaration_update")[0].levelno == logging.WARNING
