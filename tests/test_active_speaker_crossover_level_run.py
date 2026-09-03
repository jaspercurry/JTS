# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from jasper.active_speaker.capture_geometry import driver_level_geometry
from jasper.active_speaker.crossover_level_run import (
    PHONE_TRANSPORT_GRACE_S,
    CrossoverLevelRunConflict,
    CrossoverLevelRunDisposition,
    CrossoverLevelRunError,
    CrossoverLevelRunFailure,
    CrossoverLevelRunStore,
    build_level_run_request,
)
from jasper.audio_measurement.ramp import MeasurementRamp

PROFILE_SHA = "a" * 64
TARGET_SHA = "b" * 64
TOPOLOGY_ID = "speaker-topology"
TARGET_ID = "mono:woofer"
GEOMETRY = driver_level_geometry("mono", "woofer", "near_field")


def _request(
    *,
    target_fingerprint: str = TARGET_SHA,
    ramp: MeasurementRamp | None = None,
):
    return build_level_run_request(
        topology_id=TOPOLOGY_ID,
        protected_profile_fingerprint=PROFILE_SHA,
        target_id=TARGET_ID,
        target_fingerprint=target_fingerprint,
        geometry=GEOMETRY,
        ramp=ramp or MeasurementRamp(allow_bounded_low_level=True),
    )


def test_request_freezes_exact_ramp_and_derives_phone_budget():
    ramp = MeasurementRamp(
        allow_bounded_low_level=True,
        safety_timeout_s=17.125,
        feed_timeout_s=7.0,
    )
    request = _request(ramp=ramp)

    assert request.measurement_ramp() == ramp
    assert request.safety_timeout_ms == 17125
    assert request.phone_hard_timeout_ms == int(
        (17.125 + PHONE_TRANSPORT_GRACE_S) * 1000
    )
    assert request.phone_hard_timeout_ms > request.safety_timeout_ms
    assert request.to_dict()["fingerprint"] == request.fingerprint
    with pytest.raises(TypeError):
        request.ramp_config["feed_timeout_s"] = 30.0


def test_request_refuses_nonfinite_timeout_as_typed_state_error():
    with pytest.raises(CrossoverLevelRunError, match="must be finite"):
        _request(
            ramp=MeasurementRamp(
                allow_bounded_low_level=True,
                safety_timeout_s=float("inf"),
            )
        )


def test_request_refuses_geometry_target_substitution():
    with pytest.raises(CrossoverLevelRunError, match="geometry"):
        build_level_run_request(
            topology_id=TOPOLOGY_ID,
            protected_profile_fingerprint=PROFILE_SHA,
            target_id="mono:tweeter",
            target_fingerprint=TARGET_SHA,
            geometry=GEOMETRY,
            ramp=MeasurementRamp(allow_bounded_low_level=True),
        )


def test_request_refuses_declared_geometry_substitution():
    request = _request()
    raw = request.to_dict()
    raw["capture_geometry"] = "reference_axis"

    with pytest.raises(CrossoverLevelRunError, match="does not match"):
        type(request)(
            topology_id=raw["topology_id"],
            protected_profile_fingerprint=raw["protected_profile_fingerprint"],
            target_id=raw["target_id"],
            target_fingerprint=raw["target_fingerprint"],
            geometry=raw["geometry"],
            capture_geometry=raw["capture_geometry"],
            ramp_config=raw["ramp_config"],
        )


def test_a_write_publishes_the_lock_group_writable(tmp_path):
    """A group member that can only READ this lock cannot take it at all.

    Holding an advisory lock opens the file for write, so a group-read lock
    shuts out every service account that does not own it -- the whole fleet
    once any root-run poll created it first (ADR-0196). A write path is what
    takes the lock; ``snapshot`` is deliberately lock-free.
    """
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")

    store.claim(_request())

    assert oct(os.stat(store.lock_path).st_mode & 0o777) == "0o660"


def test_snapshot_reads_without_taking_the_file_lock(tmp_path):
    """The poll path must not open the advisory lock; a bare read cannot create it.

    This is the ~3 s ERROR loop's root cause: a lock-taking read opened a
    sibling lock every poll, and on a fresh box created it -- so a record that
    was merely absent surfaced as a permission fault. Lock-free of the FILE
    lock, the absent record is just "no run" (ADR-0196 decision 1).
    """
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")

    assert store.snapshot() is None
    assert not store.lock_path.exists()


def test_snapshot_reads_the_record_and_flag_under_the_thread_lock(tmp_path):
    """Fix for a torn read the lock-free read introduced.

    ``_finish`` publishes the file record and the in-memory
    ``_live_succeeded_run_id`` flag as a PAIR under ``_THREAD_LOCK``. Reading
    the two without it can see the record SUCCEEDED one ~1.5 s cycle before the
    flag catches up. So ``snapshot`` must hold ``_THREAD_LOCK`` (the in-process
    lock) across its read -- but NOT the advisory FILE lock (the ADR-0196
    point). Probe from another thread: while ``_read`` runs, ``_THREAD_LOCK``
    must be unacquirable.
    """
    import jasper.active_speaker.crossover_level_run as clr

    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    held_during_read: list[bool] = []
    original_read = store._read

    def _probe_from_other_thread() -> None:
        # _THREAD_LOCK is an RLock (reentrant), so it must be probed from a
        # DIFFERENT thread to tell whether snapshot's own thread holds it.
        got = clr._THREAD_LOCK.acquire(blocking=False)
        if got:
            clr._THREAD_LOCK.release()
        held_during_read.append(not got)

    def _read_while_probing() -> dict:
        probe = threading.Thread(target=_probe_from_other_thread)
        probe.start()
        probe.join()
        return original_read()

    store._read = _read_while_probing  # type: ignore[method-assign]
    store.snapshot()

    # A different thread could NOT take _THREAD_LOCK while _read ran.
    assert held_during_read == [True]


def test_concurrent_identical_claims_dispatch_exactly_once(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    request = _request()

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _index: store.claim(request), range(16)))

    assert sum(claim.should_dispatch for claim in claims) == 1
    assert {claim.run_id for claim in claims} == {claims[0].run_id}
    assert {claim.disposition for claim in claims} == {
        CrossoverLevelRunDisposition.NEW,
        CrossoverLevelRunDisposition.DUPLICATE_ACTIVE,
    }


def test_active_different_request_refuses_instead_of_replacing(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    first = store.claim(_request())

    with pytest.raises(CrossoverLevelRunConflict, match="already active"):
        store.claim(_request(target_fingerprint="c" * 64))

    assert store.snapshot()["run_id"] == first.run_id
    assert store.snapshot()["target_fingerprint"] == TARGET_SHA


def test_backend_consumes_frozen_config_once(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    request = _request(
        ramp=MeasurementRamp(
            allow_bounded_low_level=True,
            safety_timeout_s=22.0,
            cap_bump_db=9.5,
        )
    )
    claim = store.claim(request)
    store.mark_phone_armed(claim.run_id)

    executed = store.begin_backend(claim.run_id, geometry=GEOMETRY)

    assert executed == request.measurement_ramp()
    assert store.snapshot()["ramp_config_fingerprint"] == (
        request.ramp_config_fingerprint
    )
    with pytest.raises(CrossoverLevelRunConflict, match="already started"):
        store.begin_backend(claim.run_id, geometry=GEOMETRY)


def test_phone_timeout_then_same_run_success_is_late_success(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())

    assert store.mark_phone_timeout(claim.run_id) is False
    assert store.mark_phone_armed(claim.run_id) is True
    store.begin_backend(claim.run_id, geometry=GEOMETRY)
    assert store.mark_phone_timeout(claim.run_id) is True
    assert store.mark_phone_timeout(claim.run_id) is False
    assert store.succeed(claim.run_id) is True

    snapshot = store.snapshot()
    assert snapshot["phase"] == "succeeded"
    assert snapshot["phone_timeout"] is True
    assert snapshot["late_success"] is True
    assert snapshot["result_available"] is True
    duplicate = store.claim(_request())
    assert duplicate.disposition is CrossoverLevelRunDisposition.DUPLICATE_SUCCEEDED
    assert duplicate.should_dispatch is False


def test_discarded_process_local_result_stops_terminal_success_deduplication(
    tmp_path,
):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())
    store.mark_phone_armed(claim.run_id)
    store.begin_backend(claim.run_id, geometry=GEOMETRY)
    assert store.succeed(claim.run_id) is True

    assert (
        store.invalidate_succeeded_result(
            geometry=driver_level_geometry("mono", "tweeter", "near_field")
        )
        is False
    )
    assert store.snapshot()["result_available"] is True
    assert store.invalidate_succeeded_result(geometry=GEOMETRY) is True
    assert store.invalidate_succeeded_result(geometry=GEOMETRY) is False
    assert store.snapshot()["result_available"] is False

    retry = store.claim(_request())
    assert retry.disposition is CrossoverLevelRunDisposition.NEW
    assert retry.run_id != claim.run_id


def test_delayed_same_run_timeout_annotates_already_persisted_success(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())
    store.mark_phone_armed(claim.run_id)
    store.begin_backend(claim.run_id, geometry=GEOMETRY)
    assert store.succeed(claim.run_id) is True

    assert store.mark_phone_timeout(claim.run_id) is True

    snapshot = store.snapshot()
    assert snapshot["phase"] == "succeeded"
    assert snapshot["phone_timeout"] is True
    assert snapshot["late_success"] is True
    assert store.mark_phone_timeout(claim.run_id) is False


def test_phone_timeout_before_backend_terminally_refuses_audio(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())
    assert store.mark_phone_armed(claim.run_id) is True

    assert store.mark_phone_timeout(claim.run_id) is True

    snapshot = store.snapshot()
    assert snapshot["phase"] == "failed"
    assert snapshot["terminal_reason"] == "phone_aborted"
    assert snapshot["phone_timeout"] is True
    with pytest.raises(CrossoverLevelRunError, match="does not own this armed run"):
        store.begin_backend(claim.run_id, geometry=GEOMETRY)


def test_success_requires_phone_and_backend_correlation(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())

    with pytest.raises(CrossoverLevelRunError, match="armed phone or backend"):
        store.succeed(claim.run_id)
    assert store.snapshot()["phase"] == "awaiting_phone"


def test_stale_terminal_updates_cannot_finish_current_run(tmp_path):
    store = CrossoverLevelRunStore(path=tmp_path / "run.json")
    claim = store.claim(_request())
    stale_id = uuid.uuid4().hex

    assert (
        store.fail(stale_id, reason=CrossoverLevelRunFailure.FINALIZATION_FAILED)
        is False
    )
    assert store.succeed(stale_id) is False
    assert store.snapshot()["run_id"] == claim.run_id
    assert store.snapshot()["phase"] == "awaiting_phone"


def test_restart_interrupts_prior_owner_and_rejects_late_completion(tmp_path):
    path = tmp_path / "run.json"
    prior = CrossoverLevelRunStore(path=path, owner_id="1" * 32)
    claim = prior.claim(_request())
    prior.mark_phone_armed(claim.run_id)

    restarted = CrossoverLevelRunStore(path=path, owner_id="2" * 32)
    with pytest.raises(CrossoverLevelRunConflict, match="prior service owner"):
        restarted.claim(_request())
    snapshot = restarted.claim_owner()

    assert snapshot["phase"] == "interrupted"
    assert snapshot["terminal_reason"] == "service_restarted"
    assert (
        prior.fail(
            claim.run_id,
            reason=CrossoverLevelRunFailure.LEVEL_MATCH_ACTION_FAILED,
        )
        is False
    )
    retry = restarted.claim(_request())
    assert retry.disposition is CrossoverLevelRunDisposition.NEW
    assert retry.run_id != claim.run_id


def test_restart_does_not_deduplicate_in_memory_success(tmp_path):
    path = tmp_path / "run.json"
    prior = CrossoverLevelRunStore(path=path, owner_id="1" * 32)
    claim = prior.claim(_request())
    prior.mark_phone_armed(claim.run_id)
    prior.begin_backend(claim.run_id, geometry=GEOMETRY)
    assert prior.succeed(claim.run_id) is True

    restarted = CrossoverLevelRunStore(path=path, owner_id="2" * 32)
    retry = restarted.claim(_request())

    assert retry.disposition is CrossoverLevelRunDisposition.NEW
    assert retry.run_id != claim.run_id


def test_public_snapshot_and_durable_file_contain_no_transport_secrets(tmp_path):
    path = tmp_path / "run.json"
    store = CrossoverLevelRunStore(path=path)
    store.claim(_request())

    public = store.snapshot()
    durable = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(durable, sort_keys=True)

    assert "ramp_config" not in public
    assert "owner_id" not in public
    assert public["result_available"] is False
    for forbidden in ("tap_link", "pull_token", "credential"):
        assert forbidden not in serialized


def test_malformed_durable_state_fails_closed(tmp_path):
    path = tmp_path / "run.json"
    path.write_text('{"schema_version": true}', encoding="utf-8")
    store = CrossoverLevelRunStore(path=path)

    with pytest.raises(CrossoverLevelRunError, match="malformed"):
        store.snapshot()
    with pytest.raises(CrossoverLevelRunError, match="malformed"):
        store.claim(_request())


def test_running_timeout_without_backend_is_rejected_as_malformed(tmp_path):
    path = tmp_path / "run.json"
    store = CrossoverLevelRunStore(path=path)
    claim = store.claim(_request())
    store.mark_phone_armed(claim.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["current"]["phone_timeout_at"] = raw["current"]["claimed_at"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CrossoverLevelRunError, match="started no backend"):
        store.begin_backend(claim.run_id, geometry=GEOMETRY)
