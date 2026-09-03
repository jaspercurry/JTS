# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import jasper.active_speaker.playback as active_playback
import jasper.correction.playback as correction_playback
from jasper.active_speaker import web_commissioning as web
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.crossover_contract import verified_driver_excitation
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.calibration_level import MIN_TEST_LEVEL_DBFS
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_profile import _two_way_preset


def _topology(**kwargs):
    return mono_output_topology(topology_name="Bench mono", **kwargs)


def _staged_anchor_for(topology, staged_path):
    """A staged-anchor stub in the shape ``staging.py`` really writes.

    The `topology` and `hardware` blocks are NOT decoration: since #2285 both
    /sound/ and /correction/ gate "already loaded" on
    ``staged_topology_match_status``, which compares them against the box's
    saved topology, so a stub carrying only `status` + `config.path` describes
    a staged pair production never produces and makes the anchor look stale.
    Mirrors ``stage_protected_startup_config``'s payload field-for-field
    (`staging.py`, the "topology"/"hardware" blocks) so a fixture cannot drift
    into agreeing with a check the real writer would fail.
    """

    return {
        "status": "staged",
        "config": {"path": staged_path},
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
            "speaker_group_id": None,
            "speaker_label": None,
            "speaker_group_ids": [],
            "speaker_labels": [],
        },
        "hardware": {
            "device_id": topology.hardware.device_id,
            "device_label": topology.hardware.device_label,
            "card_id": topology.hardware.card_id,
            "physical_output_count": topology.hardware.physical_output_count,
            "clock_domain_id": topology.hardware.clock_domain_id,
        },
        # Written out from the topology's own channels rather than by calling
        # `topology_target_signature`: this fixture mirrors the WRITER, and a
        # fixture that called the comparator's own helper would agree with it by
        # construction even if the writer had stopped producing this shape.
        "targets": [
            {
                "speaker_group_id": group.id,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "identity_verified": bool(channel.identity_verified),
                "startup_muted": bool(channel.startup_muted),
                "protection_required": bool(channel.protection_required),
                "protection_status": channel.protection_status,
            }
            for group in topology.speaker_groups
            for channel in group.channels
        ],
    }


def _durable_driver_record(
    topology,
    *,
    role="woofer",
    playback_id="play-woofer",
    test_level_dbfs=-72.0,
):
    target = next(
        item for item in active_driver_targets(topology) if item["role"] == role
    )
    return {
        "captured": True,
        "target_id": target["target_id"],
        "target_fingerprint": target["target_fingerprint"],
        "speaker_group_id": target["speaker_group_id"],
        "role": role,
        "output_index": target["output_index"],
        "outcome": "heard_correct_driver",
        "playback_id": playback_id,
        "test_level_dbfs": test_level_dbfs,
        "floor_confirmation": {
            "accepted": True,
            "playback_id": playback_id,
            "target": {
                "speaker_group_id": target["speaker_group_id"],
                "role": role,
                "output_index": target["output_index"],
            },
        },
        "issues": [],
    }


def _driver_comparison_set(topology):
    from jasper.active_speaker.capture_geometry import comparison_set_fingerprint

    core = {
        "schema_version": 2,
        "comparison_set_id": "1" * 32,
        "created_at": "2026-07-11T12:00:00Z",
        "topology_id": topology.topology_id,
        "profile_context_id": "profile-1",
        "setup_sha256": "2" * 64,
        "device_sha256": "3" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            target["target_id"]: {
                "target_id": target["target_id"],
                "speaker_group_id": target["speaker_group_id"],
                "role": target["role"],
                "tone_frequency_hz": (
                    250.0 if target["role"] == "woofer" else 6250.0
                ),
                "tone_peak_dbfs": -12.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -4.0,
            }
            for target in active_driver_targets(topology)
        },
    }
    return {**core, "fingerprint": comparison_set_fingerprint(core)}


def _install_driver_admission_prerequisites(monkeypatch):
    """Keep legacy orchestration tests focused below the new admission gates."""

    from jasper import dsp_apply
    from jasper.active_speaker import design_draft

    monkeypatch.setattr(
        design_draft,
        "load_design_draft",
        lambda: {"driver_safety_profile": {"status": "confirmed"}},
    )

    @asynccontextmanager
    async def unlocked(*_args, **_kwargs):
        yield

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", unlocked)


def test_driver_capture_sweep_requires_confirmed_driver(monkeypatch):
    monkeypatch.setattr(web, "load_output_topology", _topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: {})

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_required"


def test_driver_capture_sweep_accepts_durable_confirmation_after_session_expiry(
    monkeypatch,
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(
        web,
        "load_safe_playback_state",
        lambda: (_ for _ in ()).throw(
            AssertionError("durable evidence must not require an armed session")
        ),
    )
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(topology=topology),
    )
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)
    loaded = {}

    async def blocked_load(**kwargs):
        loaded.update(kwargs)
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "test_stop_after_durable_gate",
                    "message": "durable gate passed",
                }],
            },
        }

    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        blocked_load,
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "driver_capture_sweep_load_failed"
    assert loaded["role"] == "woofer"


@pytest.mark.parametrize(
    "malformation",
    ["missing", "unaccepted", "playback_mismatch", "target_mismatch"],
)
def test_driver_capture_refuses_malformed_durable_floor_confirmation(
    monkeypatch,
    malformation,
):
    topology = _topology()
    record = _durable_driver_record(topology)
    if malformation == "missing":
        record.pop("floor_confirmation")
    elif malformation == "unaccepted":
        record["floor_confirmation"]["accepted"] = False
    elif malformation == "playback_mismatch":
        record["floor_confirmation"]["playback_id"] = "another-playback"
    elif malformation == "target_mismatch":
        record["floor_confirmation"]["target"]["role"] = "tweeter"
    measurements = {
        "summary": {
            "latest_driver_measurements": {"mono:woofer": record},
            "latest_driver_confirmations": {"mono:woofer": record},
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_invalid"
    assert payload["audio_emitted"] is False


def test_driver_capture_refuses_topology_stale_floor_record(monkeypatch):
    topology = _topology()
    # `load_measurement_state(topology)` excludes the old target fingerprint
    # from latest_driver_measurements and reports it only as stale evidence.
    measurements = {
        "summary": {
            "latest_driver_measurements": {},
            "stale_driver_record_count": 1,
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_required"
    assert payload["audio_emitted"] is False


def _applied_excitation_profile(
    *,
    topology=None,
    topology_id=None,
    topology_fingerprint_value=None,
    gain_db=-9.0,
):
    topology = topology or _topology()
    return {
        "status": "applied",
        "baseline_id": "baseline-1",
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology_id or topology.topology_id,
            "topology_fingerprint": (
                topology_fingerprint_value
                or topology_config_fingerprint(topology)
            ),
            "preset": _two_way_preset(),
            "playback_device": "hw:Loopback,1,0",
            "corrections": {
                "woofer": {
                    "gain_db": gain_db,
                    "delay_ms": 0.25,
                    "inverted": False,
                },
                "tweeter": {
                    "gain_db": -3.0,
                    "delay_ms": 0.0,
                    "inverted": True,
                },
            },
        },
    }


def test_automatic_driver_excitation_uses_current_applied_snapshot():
    topology = _topology()
    payload = web.automatic_driver_excitation(
        topology,
        "woofer",
        applied_profile=_applied_excitation_profile(
            topology=topology,
            gain_db=-9.5,
        ),
    )

    assert payload == {
        "status": "ready",
        "schema_version": 1,
        "scope": "sweep_plus_role_varying_commission_gain",
        "sweep_peak_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
        "commissioning_gain_db": -9.5,
        "effective_peak_dbfs": -21.5,
        "gain_source": web.AUTOMATIC_EXCITATION_GAIN_SOURCE,
        "baseline_id": "baseline-1",
        "topology_id": topology.topology_id,
        "role": "woofer",
    }
    assert verified_driver_excitation(payload) is not None


def test_automatic_driver_excitation_includes_driver_level_lock():
    topology = _topology()
    payload = web.automatic_driver_excitation(
        topology,
        "woofer",
        applied_profile=_applied_excitation_profile(
            topology=topology,
            gain_db=-9.5,
        ),
        locked_main_volume_db=-4.0,
    )

    assert payload["scope"] == "sweep_plus_role_gain_and_driver_level_lock"
    assert payload["locked_main_volume_db"] == -4.0
    assert payload["effective_peak_dbfs"] == -25.5
    assert verified_driver_excitation(payload) is not None

    played = web._played_excitation_ledger(
        payload,
        {"amplitude_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS},
    )
    assert verified_driver_excitation(played) is not None


@pytest.mark.parametrize(
    ("applied_profile", "reason"),
    [
        ({}, "active_crossover_profile_not_applied"),
        (
            _applied_excitation_profile(topology_id="stale-topology"),
            "active_applied_profile_snapshot_topology_stale",
        ),
        (
            {
                **_applied_excitation_profile(),
                "recomposition_snapshot": {"schema_version": 1},
            },
            "active_applied_profile_snapshot_domain_invalid",
        ),
    ],
)
def test_automatic_driver_excitation_rejects_missing_or_stale_snapshot(
    applied_profile, reason
):
    payload = web.automatic_driver_excitation(
        _topology(),
        "woofer",
        applied_profile=applied_profile,
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == reason


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ("different_fingerprint", "active_applied_profile_snapshot_topology_stale"),
        ("partial_corrections", "active_applied_profile_snapshot_invalid"),
        ("malformed_correction", "active_applied_profile_snapshot_invalid"),
        ("unsafe_gain", "active_applied_profile_snapshot_invalid"),
        ("missing_playback_device", "active_applied_profile_snapshot_invalid"),
        ("invalid_preset", "active_applied_profile_snapshot_invalid"),
    ],
)
def test_automatic_capture_refuses_noncanonical_applied_snapshot(variant, reason):
    topology = _topology()
    profile = _applied_excitation_profile(topology=topology)
    snapshot = profile["recomposition_snapshot"]
    if variant == "different_fingerprint":
        snapshot["topology_fingerprint"] = "different-current-topology"
    elif variant == "partial_corrections":
        snapshot["corrections"].pop("tweeter")
    elif variant == "malformed_correction":
        snapshot["corrections"]["woofer"]["delay_ms"] = -1.0
    elif variant == "unsafe_gain":
        snapshot["corrections"]["woofer"]["gain_db"] = -60.1
    elif variant == "missing_playback_device":
        snapshot.pop("playback_device")
    elif variant == "invalid_preset":
        snapshot["preset"] = {}

    driver = web.automatic_driver_excitation(
        topology,
        "woofer",
        applied_profile=profile,
    )

    assert driver["status"] == "blocked"
    assert driver["reason"] == reason


@pytest.mark.parametrize("legacy_floor_dbfs", [-20.0, -60.0])
@pytest.mark.parametrize("applied_gain_db", [-9.0, 0.0])
def test_driver_capture_sweep_never_reuses_legacy_floor_level(
    monkeypatch, legacy_floor_dbfs, applied_gain_db
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(
                    topology,
                    test_level_dbfs=legacy_floor_dbfs,
                ),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(
                    topology,
                    test_level_dbfs=legacy_floor_dbfs,
                ),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(
            topology=topology,
            gain_db=applied_gain_db,
        ),
    )
    load_call = {}

    async def fake_load(**kwargs):
        load_call.update(kwargs)
        return {"load": {"status": "loaded"}}

    play_call = {}

    async def fake_play(**kwargs):
        play_call.update(kwargs)
        return SimpleNamespace(
            sweep_meta=SimpleNamespace(
                to_dict=lambda: {
                    "amplitude_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
                }
            ),
            handoff=SimpleNamespace(
                admission_id="admission-woofer",
                to_dict=lambda: {"admission_id": "admission-woofer"},
            ),
        )

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", fake_load)
    monkeypatch.setattr(web, "commission_seams", lambda _cam: (None, None, None))
    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", lambda *_a: {})
    monkeypatch.setattr(web, "_commission_tone_release_fanin_lane", lambda **_kw: None)
    from jasper.active_speaker import commissioning_admission

    monkeypatch.setattr(
        commissioning_admission, "play_admitted_driver_capture", fake_play
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "completed"
    assert payload["test_level_dbfs"] == applied_gain_db
    assert load_call["level_dbfs"] == applied_gain_db
    assert load_call["volume_limit_db"] == -4.0
    startup_gate = load_call["startup_gate_calibration_level"]
    assert startup_gate["status"] == "floor"
    assert startup_gate["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS
    )
    assert play_call["commissioning_gain_db"] == applied_gain_db
    assert play_call["expected_main_volume_db"] == -4.0
    assert payload["test_level_dbfs"] != legacy_floor_dbfs


def test_commission_tone_select_fanin_lane_indeterminate_recovery_standalone(
    monkeypatch,
):
    """SELECT response lost (mux command raises): standalone mode's recovery
    releases its OWN owner — never correction's gate."""

    calls: list[str] = []

    def flaky_mux_command(cmd: str) -> dict:
        calls.append(cmd)
        if len(calls) == 1:
            raise RuntimeError("response lost")
        return {"active_source": None}

    monkeypatch.setattr(web, "_commission_tone_mux_command", flaky_mux_command)

    with pytest.raises(RuntimeError, match="response lost"):
        web._commission_tone_select_fanin_lane()

    assert calls == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_commission_tone_select_fanin_lane_indeterminate_recovery_nested(
    monkeypatch,
):
    """SELECT response lost while nested under a correction measurement
    window: recovery must NOT release the outer owner's gate — it restores
    the outer owner's prior label instead (still a TEST_SELECT, never a
    TEST_RELEASE)."""

    calls: list[str] = []

    def flaky_mux_command(cmd: str) -> dict:
        calls.append(cmd)
        if len(calls) == 1:
            raise RuntimeError("response lost")
        return {"active_source": "correction"}

    monkeypatch.setattr(web, "_commission_tone_mux_command", flaky_mux_command)
    fanin_gate_context = web.FaninGateContext(
        owner="correction-measurement", restore_label="correction",
    )

    with pytest.raises(RuntimeError, match="response lost"):
        web._commission_tone_select_fanin_lane(fanin_gate_context)

    assert calls == [
        "TEST_SELECT correction correction-measurement",
        "TEST_SELECT correction correction-measurement",
    ]
    assert all("TEST_RELEASE" not in call for call in calls)


def test_async_commission_tone_mux_command_runs_off_event_loop(monkeypatch):
    worker_thread_ids: list[int] = []

    def command() -> dict:
        worker_thread_ids.append(threading.get_ident())
        return {"status": "ok"}

    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", command)

    async def scenario() -> tuple[int, dict]:
        loop_thread_id = threading.get_ident()
        payload = await web._commission_tone_select_fanin_lane_async()
        return loop_thread_id, payload

    loop_thread_id, payload = asyncio.run(scenario())

    assert payload == {"status": "ok"}
    assert worker_thread_ids
    assert worker_thread_ids[0] != loop_thread_id


@pytest.mark.parametrize(
    ("fanin_gate_context", "expected_calls"),
    [
        (
            None,
            [
                "TEST_SELECT correction active-speaker-commissioning",
                "TEST_RELEASE active-speaker-commissioning",
            ],
        ),
        (
            web.FaninGateContext(
                owner="correction-measurement",
                restore_label="correction",
            ),
            [
                "TEST_SELECT correction correction-measurement",
                "TEST_SELECT correction correction-measurement",
            ],
        ),
    ],
)
def test_async_commission_tone_select_cancellation_settles_and_releases_gate(
    monkeypatch,
    fanin_gate_context,
    expected_calls,
):
    """Cancellation cannot orphan a late successful mux TEST_SELECT.

    ``asyncio.to_thread`` cannot stop its worker. Model the mux committing the
    selection only after caller cancellation, then cancel the caller again
    while release/restore is blocked. Cancellation must not propagate until
    the same owner has given the standalone gate back or restored the nested
    outer label.
    """

    select_started = threading.Event()
    allow_select_response = threading.Event()
    cleanup_started = threading.Event()
    allow_cleanup_response = threading.Event()
    calls: list[str] = []

    def delayed_mux_command(cmd: str) -> dict:
        calls.append(cmd)
        if len(calls) == 1:
            select_started.set()
            assert allow_select_response.wait(timeout=2.0)
        else:
            cleanup_started.set()
            assert allow_cleanup_response.wait(timeout=2.0)
        return {"active_source": "correction"}

    monkeypatch.setattr(web, "_commission_tone_mux_command", delayed_mux_command)

    async def wait_for_thread_event(event: threading.Event) -> None:
        while not event.is_set():
            await asyncio.sleep(0)

    async def scenario() -> None:
        task = asyncio.create_task(
            web._commission_tone_select_fanin_lane_async(fanin_gate_context)
        )
        await wait_for_thread_event(select_started)

        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False

        allow_select_response.set()
        await wait_for_thread_event(cleanup_started)

        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False

        allow_cleanup_response.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert calls == expected_calls


def _driver_capture_sweep_boundary(monkeypatch, *, play_admitted=None):
    """Boundary mocks for a play_driver_capture_sweep() run that leave the
    REAL _commission_tone_select/release_fanin_lane() in place — only the
    lowest-level socket call (_commission_tone_mux_command) is faked — so
    tests can inspect the actual TEST_SELECT/TEST_RELEASE strings sent to
    jasper-mux for standalone vs nested (FaninGateContext) commissioning.
    """
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(topology=topology),
    )

    async def fake_load(**kwargs):
        return {"load": {"status": "loaded"}}

    async def default_play_admitted(**kwargs):
        return SimpleNamespace(
            sweep_meta=SimpleNamespace(
                to_dict=lambda: {"amplitude_dbfs": -12.0}
            ),
            handoff=SimpleNamespace(
                admission_id="admission-woofer",
                to_dict=lambda: {"admission_id": "admission-woofer"},
            ),
        )

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", fake_load)
    monkeypatch.setattr(web, "commission_seams", lambda _cam: (None, None, None))
    from jasper.active_speaker import commissioning_admission

    monkeypatch.setattr(
        commissioning_admission,
        "play_admitted_driver_capture",
        play_admitted or default_play_admitted,
    )

    mux_calls: list[str] = []

    def fake_mux_command(cmd: str) -> dict:
        mux_calls.append(cmd)
        return {"active_source": "correction"}

    monkeypatch.setattr(web, "_commission_tone_mux_command", fake_mux_command)
    return mux_calls


def test_driver_capture_sweep_standalone_mode_owns_and_releases_its_own_gate(
    monkeypatch,
):
    """No FaninGateContext (today's /sound/ commissioning path): unchanged
    behavior — claims its own owner and releases it, never touching
    correction's gate."""

    mux_calls = _driver_capture_sweep_boundary(monkeypatch)

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "completed"
    assert mux_calls == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_driver_capture_sweep_nested_mode_selects_and_restores_under_outer_owner(
    monkeypatch,
):
    """FaninGateContext set (the crossover-driver-sweep capture flow, running
    inside a correction measurement window): the tone selects under the
    OUTER owner (never its own 'active-speaker-commissioning') and, on
    completion, relabels back to the outer owner's prior label — never a
    TEST_RELEASE. The gate stays continuously held by one owner across the
    window; the coordinator's own end-of-window release is the only
    release. This is the PR #1508 regression pin: before the fix, the
    second-owner SELECT was refused with 'test fan-in gate is owned by
    correction-measurement' (hardware-observed on JTS3)."""

    mux_calls = _driver_capture_sweep_boundary(monkeypatch)
    fanin_gate_context = web.FaninGateContext(
        owner="correction-measurement", restore_label="correction",
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
            fanin_gate_context=fanin_gate_context,
        )
    )

    assert payload["status"] == "completed"
    assert mux_calls == [
        "TEST_SELECT correction correction-measurement",
        "TEST_SELECT correction correction-measurement",
    ]
    assert all("TEST_RELEASE" not in call for call in mux_calls)
    assert all("correction-measurement" in call for call in mux_calls)


def test_driver_capture_sweep_nested_mode_restores_label_on_crash(monkeypatch):
    """Crash mid-tone (the admitted capture raises) still restores the outer
    owner's label via the finally block — the nested gate is never left
    dangling on an unhandled exception, and it is still a restore, not a
    release."""

    async def crashing_play_admitted(**kwargs):
        raise RuntimeError("simulated mid-tone crash")

    mux_calls = _driver_capture_sweep_boundary(
        monkeypatch, play_admitted=crashing_play_admitted,
    )
    fanin_gate_context = web.FaninGateContext(
        owner="correction-measurement", restore_label="correction",
    )

    with pytest.raises(RuntimeError, match="simulated mid-tone crash"):
        asyncio.run(
            web.play_driver_capture_sweep(
                {"speaker_group_id": "mono", "role": "woofer"},
                camilla_factory=lambda: object(),
                locked_main_volume_db=-4.0,
                fanin_gate_context=fanin_gate_context,
            )
        )

    assert mux_calls == [
        "TEST_SELECT correction correction-measurement",
        "TEST_SELECT correction correction-measurement",
    ]
    assert all("TEST_RELEASE" not in call for call in mux_calls)


@pytest.mark.parametrize(
    ("post_play_reason", "expected_issue"),
    [
        ("main_volume_drift", "active_driver_capture_volume_drift"),
        (
            "post_play_volume_unverified",
            "active_driver_capture_post_play_volume_unverified",
        ),
        (
            "post_play_volume_verification_cancelled",
            "active_driver_capture_post_play_volume_unverified",
        ),
    ],
)
def test_driver_capture_post_play_failure_reports_consumed_attempt_and_reason(
    monkeypatch,
    post_play_reason,
    expected_issue,
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)
    from jasper.active_speaker import baseline_profile, commissioning_admission

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(topology=topology, gain_db=-9.0),
    )

    async def fake_load(**_kwargs):
        return {"load": {"status": "loaded"}}

    artifact = ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning_bundle",
        bundle_id="bundle-1",
        relative_path="admission/v1/playback/attempt-1.json",
        sha256="a" * 64,
        byte_size=100,
    )

    async def drift_after_play(**_kwargs):
        raise commissioning_admission.ActiveCommissioningPlaybackDrift(
            "main volume changed during admitted driver playback",
            reason=post_play_reason,
            admission_id="attempt-1",
            playback_artifact=artifact,
        )

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", fake_load)
    monkeypatch.setattr(web, "commission_seams", lambda _cam: (None, None, None))
    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", lambda *_a: {})
    monkeypatch.setattr(web, "_commission_tone_release_fanin_lane", lambda **_kw: None)
    monkeypatch.setattr(
        commissioning_admission, "play_admitted_driver_capture", drift_after_play
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "failed"
    assert payload["reason"] == expected_issue
    assert payload["audio_emitted"] is True
    assert payload["playback"]["audio_may_have_started"] is True
    assert payload["playback"]["post_play_failure_reason"] == post_play_reason
    assert payload["issues"][0]["code"] == expected_issue
    expected_consumed_attempt = {
        "admission_id": "attempt-1",
        "playback_artifact": artifact.to_dict(),
        "requires_new_generation": True,
    }
    assert payload["capture_admission"] == expected_consumed_attempt
    assert payload["playback"]["capture_admission"] == expected_consumed_attempt


def test_automatic_driver_load_captures_entry_before_startup_anchor(monkeypatch):
    entry_path = "/var/lib/camilladsp/configs/sound_current.yml"

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return entry_path

    monkeypatch.setattr(web, "load_staged_startup_config", lambda: {"status": "staged"})

    anchor_call = {}

    async def ensure_anchor(**kwargs):
        anchor_call.update(kwargs)
        return {"status": "loaded"}

    monkeypatch.setattr(web, "_ensure_commission_startup_anchor", ensure_anchor)
    monkeypatch.setattr(
        web,
        "write_commission_path_safety",
        lambda *_args, **_kwargs: "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        web,
        "commission_seams",
        lambda _cam: (object(), object(), object()),
    )

    load_driver_call = {}

    async def load_driver(*_args, **kwargs):
        load_driver_call.update(kwargs)
        return {"load": {"status": "loaded"}}

    monkeypatch.setattr(web, "load_driver_commissioning_config", load_driver)

    frozen_preset = object()
    payload = asyncio.run(
        web._load_driver_commissioning_config_for_level(
            topology=_topology(),
            speaker_group_id="mono",
            role="woofer",
            level_dbfs=0.0,
            volume_limit_db=-4.0,
            startup_gate_calibration_level={"status": "floor"},
            preset=frozen_preset,
            crossover_preview=None,
            camilla_factory=Cam,
        )
    )

    assert payload["measurement_transaction"] == {
        "kind": "automatic_driver_capture",
        "entry_config_path": entry_path,
        "entry_config_error": None,
        "restored": False,
    }
    assert anchor_call["preset"] is frozen_preset
    assert anchor_call["crossover_preview"] is None
    assert load_driver_call["volume_limit_db"] == -4.0
    # De-anchoring live production must durably stash its path FIRST, so the
    # sequence-level restore has a crash-safe target.
    from jasper.active_speaker import capture_entry_anchor

    assert capture_entry_anchor.pending_entry() == entry_path
    # _ensure_commission_startup_anchor reporting status="loaded" means it just
    # reloaded the all-muted anchor and already triggered
    # jasper-audio-hardware-reconcile for this exact DAC a moment ago (see
    # startup_load._trigger_audio_hardware_reconcile). A second reconcile
    # immediately behind it is redundant -- hardware-reproduced on JTS3
    # 2026-07-16 (deterministic 2/2 aplay timeouts on the driver capture
    # sweep), where an automatic capture retry always re-enters this reload
    # branch (the flow's own cleanup, _restore_automatic_driver_entry_config,
    # reverts CamillaDSP's persisted config path to the pre-commissioning
    # production config after every attempt) and paid for two
    # jasper-audio-hardware-reconcile round trips per attempt even though
    # every run reported env_changed=0 render_changed=0.
    assert load_driver_call["reconcile_output_hardware"] is False


def test_automatic_driver_load_skips_second_reconcile_only_after_fresh_anchor_reload(
    monkeypatch,
):
    """already_loaded (anchor fast path, no reload) must still reconcile once.

    Companion to the "loaded" case above: when
    ``_ensure_commission_startup_anchor`` takes its already-anchored fast path
    (nothing needed reloading, so it did not just trigger a reconcile),
    ``load_driver_commissioning_config`` must still be allowed its own
    reconcile -- there is no recent confirmation of this exact hardware to
    piggyback on.
    """

    entry_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return entry_path

    monkeypatch.setattr(web, "load_staged_startup_config", lambda: {"status": "staged"})

    async def ensure_anchor(**_kwargs):
        return {"status": "already_loaded", "staged_config_path": entry_path}

    monkeypatch.setattr(web, "_ensure_commission_startup_anchor", ensure_anchor)
    monkeypatch.setattr(
        web,
        "write_commission_path_safety",
        lambda *_args, **_kwargs: "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        web,
        "commission_seams",
        lambda _cam: (object(), object(), object()),
    )

    load_driver_call = {}

    async def load_driver(*_args, **kwargs):
        load_driver_call.update(kwargs)
        return {"load": {"status": "loaded"}}

    monkeypatch.setattr(web, "load_driver_commissioning_config", load_driver)

    asyncio.run(
        web._load_driver_commissioning_config_for_level(
            topology=_topology(),
            speaker_group_id="mono",
            role="woofer",
            level_dbfs=0.0,
            volume_limit_db=-4.0,
            startup_gate_calibration_level={"status": "floor"},
            preset=object(),
            crossover_preview=None,
            camilla_factory=Cam,
        )
    )

    assert load_driver_call["reconcile_output_hardware"] is True


def test_capture_retry_reuses_staged_anchor_without_reanchoring(monkeypatch):
    """A retry attempt must not re-load the staged anchor (zero SetConfigs).

    Per-attempt teardown leaves the persisted path ON the staged anchor and
    the sequence's production path stashed. The next attempt then takes the
    REAL ``_ensure_commission_startup_anchor`` fast path: no
    ``set_config_file_path`` call at all before the commissioning load, and
    the stash keeps the original production path. Restoring production per
    attempt forced anchor reload + commissioning load ~150 ms apart before
    every retry's ``aplay`` — the double config swap behind the JTS3
    2026-07-16 deterministic sweep timeouts.
    """

    from jasper.active_speaker import capture_entry_anchor

    staged_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"
    production_path = "/var/lib/camilladsp/configs/sound_current.yml"
    capture_entry_anchor.record_entry(production_path)  # attempt 1 stashed it

    set_calls = []

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return staged_path  # attempt 1's teardown left the anchor loaded

        async def set_config_file_path(self, path, *, best_effort):
            set_calls.append(path)
            return True

    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web,
        "load_staged_startup_config",
        lambda: _staged_anchor_for(topology, staged_path),
    )
    monkeypatch.setattr(
        web,
        "write_commission_path_safety",
        lambda *_args, **_kwargs: "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        web,
        "commission_seams",
        lambda _cam: (object(), object(), object()),
    )

    load_driver_call = {}

    async def load_driver(*_args, **kwargs):
        load_driver_call.update(kwargs)
        return {"load": {"status": "loaded"}}

    monkeypatch.setattr(web, "load_driver_commissioning_config", load_driver)

    payload = asyncio.run(
        web._load_driver_commissioning_config_for_level(
            topology=_topology(),
            speaker_group_id="mono",
            role="woofer",
            level_dbfs=0.0,
            volume_limit_db=-4.0,
            startup_gate_calibration_level={"status": "floor"},
            preset=object(),
            crossover_preview=None,
            camilla_factory=Cam,
        )
    )

    # Real fast path: the anchor is already the persisted path, so nothing
    # was staged or reloaded — zero SetConfigFilePath calls this attempt.
    assert payload["startup_setup"]["status"] == "already_loaded"
    assert set_calls == []
    # The stash still holds the SEQUENCE's production path — an anchored
    # attempt must not overwrite it with the anchor.
    assert capture_entry_anchor.pending_entry() == production_path
    assert load_driver_call["load_config"] is not None


def test_a_path_match_with_a_stale_topology_is_not_already_loaded(monkeypatch):
    """#2285: /correction/ gates the anchor on TWO terms, like /sound/ does.

    The fast path used to short-circuit on the config-path term alone, so a box
    whose saved topology had moved since staging -- a DAC swap, a role edit --
    reused a staged graph built for hardware it no longer has, anchoring a
    commissioning rollback to the wrong speaker's graph. /sound/ has gated on
    both terms since the jts5 2026-08-06 regression; this is the same gate.

    A mismatch is a RE-STAGE, not a refusal, which is why this asserts on the
    absence of ``already_loaded`` rather than on a blocker code: the caller
    falls through and rebuilds the pair against the topology the box has.
    """

    staged_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"
    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)

    stale = _staged_anchor_for(topology, staged_path)
    stale["topology"] = dict(stale["topology"], topology_id="a-topology-since-replaced")
    matched = _staged_anchor_for(topology, staged_path)

    # THE GATE ITSELF IS DRIVEN, not its two predicates. An earlier version of
    # this pin asserted `same_config_file(p, p)` (a string against itself)
    # and `staged_topology_match_status(...)` — a helper this change does not
    # touch — and never called `_ensure_commission_startup_anchor` at all, so
    # reverting the port left it green. Composition is the subject; the
    # predicates were already covered by their own module's tests.
    #
    # The spy is enough because the fast path returns BEFORE `_stage_startup_config`
    # is reached, so "was it called" IS "was the fast path skipped" — no real
    # staging, no `preset` plumbing, and the re-stage is proved rather than
    # inferred from the absence of `already_loaded`.
    staged_calls: list[str] = []

    def _spy_stage(_topology, *, preset=None, crossover_preview=None):
        staged_calls.append("staged")
        return {"status": "blocked"}

    monkeypatch.setattr(web, "_stage_startup_config", _spy_stage)
    monkeypatch.setattr(
        web, "ensure_missing_software_guards", lambda: (topology, False)
    )

    def _run(staged_config):
        staged_calls.clear()
        return asyncio.run(
            web._ensure_commission_startup_anchor(
                group="mono",
                role="woofer",
                staged_config=staged_config,
                current_config_path=staged_path,  # the PATH term IS satisfied
                camilla_factory=object,
                preset=None,
                crossover_preview=None,
            )
        )

    # Path true, topology FALSE -> no fast path, and a RE-STAGE really happened.
    stale_result = _run(stale)
    assert stale_result.get("status") != "already_loaded", stale_result
    assert staged_calls == ["staged"]

    # Both terms true -> the fast path, and nothing is re-staged. Without this
    # half a gate that never took the fast path would also pass.
    matched_result = _run(matched)
    assert matched_result.get("status") == "already_loaded", matched_result
    assert staged_calls == []


def test_restore_pending_capture_entry_config_restores_exactly_once(monkeypatch, tmp_path):
    from jasper.active_speaker import capture_entry_anchor

    staged_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"
    entry = tmp_path / "sound_current.yml"
    entry.write_text("devices: {}\n", encoding="utf-8")
    capture_entry_anchor.record_entry(str(entry))

    set_calls = []

    class Cam:
        current = staged_path

        async def get_config_file_path(self, *, best_effort):
            return type(self).current

        async def set_config_file_path(self, path, *, best_effort):
            set_calls.append(path)
            type(self).current = path
            return True

    monkeypatch.setattr(
        web,
        "load_staged_startup_config",
        lambda: {"status": "staged", "config": {"path": staged_path}},
    )

    first = asyncio.run(
        web.restore_pending_capture_entry_config(camilla_factory=Cam)
    )
    assert first == {"status": "restored", "config_path": str(entry)}
    assert set_calls == [str(entry)]
    assert capture_entry_anchor.pending_entry() is None

    second = asyncio.run(
        web.restore_pending_capture_entry_config(camilla_factory=Cam)
    )
    assert second == {"status": "idle"}
    assert set_calls == [str(entry)]  # exactly once


def test_restore_pending_capture_entry_config_defers_and_supersedes(
    monkeypatch, tmp_path
):
    """Unreachable Camilla retains the stash; a repointed production clears it."""

    from jasper.active_speaker import capture_entry_anchor

    staged_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"
    entry = tmp_path / "sound_current.yml"
    entry.write_text("devices: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        web,
        "load_staged_startup_config",
        lambda: {"status": "staged", "config": {"path": staged_path}},
    )

    # Camilla unreachable -> deferred, stash retained (muted-safe posture).
    capture_entry_anchor.record_entry(str(entry))

    class UnreachableCam:
        async def get_config_file_path(self, *, best_effort):
            raise RuntimeError("camilla down")

        async def set_config_file_path(self, path, *, best_effort):
            raise AssertionError("must not load while state is unknown")

    deferred = asyncio.run(
        web.restore_pending_capture_entry_config(camilla_factory=UnreachableCam)
    )
    assert deferred["status"] == "deferred"
    assert capture_entry_anchor.pending_entry() == str(entry)

    # Persisted path is no longer the staged anchor (an apply repointed
    # production) -> the stale stash is cleared WITHOUT touching CamillaDSP.
    class RepointedCam:
        async def get_config_file_path(self, *, best_effort):
            return "/var/lib/camilladsp/configs/newly_applied.yml"

        async def set_config_file_path(self, path, *, best_effort):
            raise AssertionError("superseded stash must not reload anything")

    superseded = asyncio.run(
        web.restore_pending_capture_entry_config(camilla_factory=RepointedCam)
    )
    assert superseded["status"] == "superseded"
    assert capture_entry_anchor.pending_entry() is None


def test_restore_pending_capture_entry_config_missing_entry_stays_muted(
    monkeypatch, tmp_path
):
    """A vanished production config clears the stash and keeps the anchor.

    Fail direction is muted-never-loud: with no valid restore target the
    speaker stays on the all-muted staged anchor rather than guessing.
    """

    from jasper.active_speaker import capture_entry_anchor

    staged_path = "/var/lib/camilladsp/configs/active_speaker_staged_startup.yml"
    capture_entry_anchor.record_entry(str(tmp_path / "deleted.yml"))

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return staged_path

        async def set_config_file_path(self, path, *, best_effort):
            raise AssertionError("must not load a missing config")

    monkeypatch.setattr(
        web,
        "load_staged_startup_config",
        lambda: {"status": "staged", "config": {"path": staged_path}},
    )

    result = asyncio.run(
        web.restore_pending_capture_entry_config(camilla_factory=Cam)
    )
    assert result["status"] == "entry_missing"
    assert capture_entry_anchor.pending_entry() is None


def test_stage_startup_config_does_not_reread_mutable_preview_for_explicit_preset(
    monkeypatch,
):
    from jasper.active_speaker import crossover_preview, design_draft

    frozen_preset = object()
    stage_call = {}
    monkeypatch.setattr(
        crossover_preview,
        "load_crossover_preview",
        lambda **_kwargs: pytest.fail(
            "explicit applied preset must not read the mutable preview"
        ),
    )
    monkeypatch.setattr(
        design_draft,
        "load_design_draft",
        lambda: pytest.fail(
            "explicit applied preset must not read the mutable design draft"
        ),
    )
    monkeypatch.setattr(
        web,
        "stage_protected_startup_config",
        lambda topology, **kwargs: (
            stage_call.update(topology=topology, **kwargs)
            or {"status": "staged"}
        ),
    )

    result = web._stage_startup_config(_topology(), preset=frozen_preset)

    assert result == {"status": "staged"}
    assert stage_call["preset"] is frozen_preset
    assert stage_call["crossover_preview"] is None


def test_stage_startup_config_without_explicit_source_preserves_preview_gate(
    monkeypatch,
):
    from jasper.active_speaker import crossover_preview, design_draft

    draft = {"status": "ready_for_review"}
    stale_preview = {"status": "stale"}
    stage_call = {}
    monkeypatch.setattr(design_draft, "load_design_draft", lambda: draft)
    monkeypatch.setattr(
        crossover_preview,
        "load_crossover_preview",
        lambda *, current_design_draft: (
            stale_preview
            if current_design_draft is draft
            else pytest.fail("preview must bind to the loaded draft")
        ),
    )
    monkeypatch.setattr(
        web,
        "stage_protected_startup_config",
        lambda topology, **kwargs: (
            stage_call.update(topology=topology, **kwargs)
            or {"status": "blocked"}
        ),
    )

    result = web._stage_startup_config(_topology())

    assert result == {"status": "blocked"}
    assert stage_call["preset"] is None
    assert stage_call["crossover_preview"] is stale_preview


def test_startup_anchor_stages_the_callers_resolved_source(monkeypatch):
    topology = _topology()
    frozen_preset = object()
    stage_call = {}
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web,
        "ensure_missing_software_guards",
        lambda: (topology, False),
    )
    monkeypatch.setattr(
        web,
        "_stage_startup_config",
        lambda current, **kwargs: (
            stage_call.update(topology=current, **kwargs)
            or {"status": "blocked"}
        ),
    )

    result = asyncio.run(
        web._ensure_commission_startup_anchor(
            group="mono",
            role="woofer",
            staged_config={"status": "blocked"},
            current_config_path="/var/lib/camilladsp/configs/sound_current.yml",
            camilla_factory=lambda: object(),
            preset=frozen_preset,
            crossover_preview=None,
        )
    )

    assert result["status"] == "blocked"
    assert stage_call == {
        "topology": topology,
        "preset": frozen_preset,
        "crossover_preview": None,
    }


def test_startup_anchor_rejects_ambiguous_graph_source_before_fast_path():
    with pytest.raises(
        ValueError,
        match="requires one resolved graph source",
    ):
        asyncio.run(
            web._ensure_commission_startup_anchor(
                group="mono",
                role="woofer",
                staged_config={
                    "config": {"path": "/tmp/already-loaded.yml"},
                },
                current_config_path="/tmp/already-loaded.yml",
                camilla_factory=lambda: object(),
                preset=object(),
                crossover_preview={"status": "ready_for_protected_staging"},
            )
        )


def test_summed_loader_threads_resolved_source_to_startup_anchor(monkeypatch):
    frozen_preset = object()
    anchor_call = {}

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            assert best_effort is False
            return "/var/lib/camilladsp/configs/sound_current.yml"

    monkeypatch.setattr(web, "load_staged_startup_config", lambda: {"status": "staged"})

    async def blocked_anchor(**kwargs):
        anchor_call.update(kwargs)
        return {"status": "blocked"}

    monkeypatch.setattr(web, "_ensure_commission_startup_anchor", blocked_anchor)

    result = asyncio.run(
        web._load_summed_commissioning_config(
            topology=_topology(),
            speaker_group_id="mono",
            level_dbfs=-12.0,
            startup_gate_calibration_level={"status": "floor"},
            preset=frozen_preset,
            crossover_preview=None,
            camilla_factory=Cam,
        )
    )

    assert result == {"status": "blocked"}
    assert anchor_call["preset"] is frozen_preset
    assert anchor_call["crossover_preview"] is None


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
def test_automatic_driver_post_anchor_exception_restores_production_config(
    monkeypatch,
    failure_type,
):
    entry_path = "/var/lib/camilladsp/configs/sound_current.yml"
    anchor_loaded = False
    restored_paths = []

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return entry_path

        async def set_config_file_path(self, path, *, best_effort):
            restored_paths.append(path)
            return True

    monkeypatch.setattr(web, "load_staged_startup_config", lambda: {"status": "staged"})

    async def ensure_anchor(**_kwargs):
        nonlocal anchor_loaded
        anchor_loaded = True
        return {"status": "loaded"}

    monkeypatch.setattr(web, "_ensure_commission_startup_anchor", ensure_anchor)
    monkeypatch.setattr(
        web,
        "write_commission_path_safety",
        lambda *_args, **_kwargs: "/tmp/path-safety.json",
    )
    monkeypatch.setattr(
        web,
        "commission_seams",
        lambda _cam: (object(), object(), object()),
    )

    async def fail_after_anchor(*_args, **_kwargs):
        raise failure_type("post-anchor failure")

    async def inner_rollback(**_kwargs):
        return {"status": "rolled_back", "config_path": "/tmp/staged.yml"}

    monkeypatch.setattr(web, "load_driver_commissioning_config", fail_after_anchor)
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", inner_rollback)

    async def scenario():
        return await web._load_driver_commissioning_config_for_level(
            topology=_topology(),
            speaker_group_id="mono",
            role="woofer",
            level_dbfs=0.0,
            startup_gate_calibration_level={"status": "floor"},
            preset=object(),
            crossover_preview=None,
            camilla_factory=Cam,
        )

    with pytest.raises(failure_type, match="post-anchor failure"):
        asyncio.run(scenario())

    assert anchor_loaded is True
    assert restored_paths == [entry_path]


def test_automatic_driver_restore_normalizes_factory_failure_and_nested_status(
    monkeypatch,
):
    entry_path = "/var/lib/camilladsp/configs/sound_current.yml"
    load_payload = {
        "measurement_transaction": {
            "kind": "automatic_driver_capture",
            "entry_config_path": entry_path,
            "restored": False,
        },
    }

    async def inner_rollback(**_kwargs):
        return {"rollback": {"status": "rolled_back"}}

    def failed_factory():
        raise RuntimeError("controller construction failed")

    log_calls = []
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", inner_rollback)
    monkeypatch.setattr(
        web,
        "log_event",
        lambda *args, **kwargs: log_calls.append((args, kwargs)),
    )

    with pytest.raises(
        web.AutomaticDriverConfigRestoreError,
        match="controller construction failed",
    ):
        asyncio.run(
            web._restore_automatic_driver_entry_config(
                load_payload,
                camilla_factory=failed_factory,
            )
        )

    assert len(log_calls) == 1
    args, kwargs = log_calls[0]
    assert args[1] == "active_speaker.automatic_driver_config_restore"
    assert kwargs["status"] == "failed"
    assert kwargs["entry_config_path"] == entry_path
    assert kwargs["inner_rollback_status"] == "rolled_back"


@pytest.mark.parametrize("restore_fails", [False, True])
def test_automatic_driver_load_failure_restores_entry_graph(
    monkeypatch,
    restore_fails,
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    entry_path = "/var/lib/camilladsp/configs/sound_current.yml"
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(topology=topology, gain_db=0.0),
    )
    _install_driver_admission_prerequisites(monkeypatch)

    async def blocked_load(**_kwargs):
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "calibration_level_not_at_floor",
                    "message": "preflight stopped after the startup anchor",
                }],
            },
            "measurement_transaction": {
                "kind": "automatic_driver_capture",
                "entry_config_path": entry_path,
                "restored": False,
            },
        }

    # The per-attempt teardown re-mutes to the staged anchor; the entry
    # production path must NOT be restored per attempt (sequence-level restore
    # owns it — see restore_pending_capture_entry_config). restore_fails now
    # exercises the anchor rollback itself failing, which must still flip the
    # attempt to failed because the driver may be left audible.
    async def inner_rollback(**_kwargs):
        if restore_fails:
            return {"rollback": {"status": "rollback_failed"}}
        return {"rollback": {"status": "blocked"}}

    restored_paths = []

    class Cam:
        async def set_config_file_path(self, path, *, best_effort):
            restored_paths.append(path)
            return True

    log_calls = []
    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", blocked_load)
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", inner_rollback)
    monkeypatch.setattr(
        web,
        "log_event",
        lambda *args, **kwargs: log_calls.append((args, kwargs)),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=Cam,
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == ("failed" if restore_fails else "blocked")
    assert restored_paths == []
    if restore_fails:
        assert payload["issues"][0]["code"] == (
            "automatic_driver_config_restore_failed"
        )
        assert any(
            args[1] == "active_speaker.automatic_driver_config_restore"
            and kwargs["status"] == "failed"
            for args, kwargs in log_calls
        )
    else:
        assert payload["rollback"]["status"] == "anchored"
        assert payload["rollback"]["pending_entry_config_path"] == entry_path
        assert payload["issues"][0]["code"] == "calibration_level_not_at_floor"


def test_driver_capture_sweep_refuses_before_loading_when_applied_gain_is_stale(
    monkeypatch,
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(
                    topology,
                    test_level_dbfs=-60.0,
                ),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(
                    topology,
                    test_level_dbfs=-60.0,
                ),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(
            topology=topology,
            topology_id="old-topology",
        ),
    )
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "refused"
    assert payload["audio_emitted"] is False
    assert payload["reason"] == "active_applied_profile_snapshot_topology_stale"


def test_driver_capture_sweep_uses_frozen_applied_preset_not_mutable_draft(
    monkeypatch,
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    applied = _applied_excitation_profile(topology=topology, gain_db=-9.0)
    mutable = _two_way_preset()
    mutable["crossover_regions"][0]["fc_hz"] = 4000.0

    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(
        web,
        "resolve_commission_inputs",
        lambda: pytest.fail("automatic sweep must not read mutable draft inputs"),
    )
    loaded = {}

    async def capture_load(**kwargs):
        loaded.update(kwargs)
        return {
            "load": {"status": "blocked", "issues": []},
            "measurement_transaction": {},
        }

    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        capture_load,
    )
    _install_driver_admission_prerequisites(monkeypatch)

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            applied_profile=applied,
            locked_main_volume_db=-4.0,
        )
    )

    assert payload["status"] == "blocked"
    assert loaded["preset"].crossover_regions[0].fc_hz == 1600.0
    assert loaded["crossover_preview"] is None
    assert mutable["crossover_regions"][0]["fc_hz"] == 4000.0


def test_driver_capture_sweep_uses_explicit_geometry_lock_in_excitation(monkeypatch):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    applied = _applied_excitation_profile(topology=topology, gain_db=-9.0)
    seen = {}

    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})

    def excitation(*_args, **kwargs):
        seen.update(kwargs)
        return {"status": "ready", "commissioning_gain_db": -9.0}

    async def capture_load(**_kwargs):
        return {"load": {"status": "blocked", "issues": []}}

    monkeypatch.setattr(web, "automatic_driver_excitation", excitation)
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        capture_load,
    )
    _install_driver_admission_prerequisites(monkeypatch)

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            applied_profile=applied,
            locked_main_volume_db=-3.5,
        )
    )

    assert payload["status"] == "blocked"
    assert seen["locked_main_volume_db"] == -3.5


@pytest.mark.parametrize("cancelled", [False, True])
def test_driver_capture_holds_one_writer_lock_through_restore(monkeypatch, cancelled):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    applied = _applied_excitation_profile(topology=topology, gain_db=-9.0)
    events = []
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)

    from jasper import dsp_apply
    from jasper.active_speaker import commissioning_admission

    @asynccontextmanager
    async def tracked_lock(*_args, **kwargs):
        assert kwargs["source"] == "active_speaker_driver_capture"
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    async def load(**kwargs):
        events.append("load")
        return {
            "load": {"status": "loaded"},
            "measurement_transaction": {
                "entry_config_path": "/tmp/entry.yml",
                "restored": False,
            },
        }

    async def admitted(**_kwargs):
        events.append("play")
        if cancelled:
            raise asyncio.CancelledError
        return SimpleNamespace(
            sweep_meta=SimpleNamespace(
                to_dict=lambda: {"amplitude_dbfs": -12.0, "duration_s": 4.0}
            ),
            handoff=SimpleNamespace(
                admission_id="admission-1",
                to_dict=lambda: {"admission_id": "admission-1"},
            ),
        )

    async def restore(_payload, *, camilla_factory):
        del camilla_factory
        events.append("restore")
        return {"status": "anchored"}

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", tracked_lock)
    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", load)
    monkeypatch.setattr(web, "commission_seams", lambda _cam: (None, None, None))
    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", lambda *_a: {})
    monkeypatch.setattr(web, "_commission_tone_release_fanin_lane", lambda **_kw: None)
    monkeypatch.setattr(
        web, "_rollback_capture_attempt_to_anchor_resilient", restore
    )
    monkeypatch.setattr(
        commissioning_admission, "play_admitted_driver_capture", admitted
    )

    operation = web.play_driver_capture_sweep(
        {"speaker_group_id": "mono", "role": "woofer"},
        camilla_factory=lambda: object(),
        applied_profile=applied,
        locked_main_volume_db=-4.0,
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(operation)
    else:
        assert asyncio.run(operation)["status"] == "completed"
    assert events == ["lock_enter", "load", "play", "restore", "lock_exit"]


def test_driver_capture_writer_timeout_refuses_before_graph_load(monkeypatch):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    _install_driver_admission_prerequisites(monkeypatch)

    from jasper import dsp_apply

    @asynccontextmanager
    async def busy_lock(*_args, **_kwargs):
        raise dsp_apply.DspWriterLockTimeout(
            "/tmp/writer.lock",
            timeout_s=3.0,
            waited_s=3.0,
            source="active_speaker_driver_capture",
        )
        yield

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", busy_lock)
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **_kwargs: pytest.fail("writer timeout must precede graph load"),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            applied_profile=_applied_excitation_profile(topology=topology),
            locked_main_volume_db=-4.0,
        )
    )
    assert payload["status"] == "refused"
    assert payload["reason"] == "active_driver_capture_writer_busy"
    assert payload["audio_emitted"] is False


@pytest.mark.parametrize("invalid_lock", (True, float("nan"), 0.1, "-3.5"))
def test_driver_capture_sweep_refuses_invalid_explicit_geometry_lock(
    monkeypatch, invalid_lock
):
    topology = _topology()
    measurements = {
        "active_comparison_set": _driver_comparison_set(topology),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _durable_driver_record(topology),
            },
            # No acoustic block -- an accurate real-shaped by-ear
            # confirmation record, so it belongs in the confirmation-only
            # index too (see measurement._latest_current_driver_confirmations).
            "latest_driver_confirmations": {
                "mono:woofer": _durable_driver_record(topology),
            },
        },
    }
    applied = _applied_excitation_profile(topology=topology, gain_db=-9.0)
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **_kwargs: pytest.fail("an invalid lock must refuse before graph load"),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
            applied_profile=applied,
            locked_main_volume_db=invalid_lock,
        )
    )

    assert payload["status"] == "refused"
    assert payload["audio_emitted"] is False
    assert payload["reason"] == "automatic_crossover_driver_level_invalid"


def test_automatic_measurement_source_peak_is_one_shared_default():
    from jasper.active_speaker import driver_acoustics
    from jasper.audio_measurement.sweep import synchronized_swept_sine
    from jasper.correction.session import SessionConfig

    sweep_default = inspect.signature(synchronized_swept_sine).parameters[
        "amplitude_dbfs"
    ].default
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert sweep_default == AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    assert driver_acoustics.DEFAULT_AMPLITUDE_DBFS == sweep_default
    assert SessionConfig().amplitude_dbfs == sweep_default


def test_summed_capture_sweep_refuses_before_session_or_graph_mutation(monkeypatch):
    armed = {}
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    monkeypatch.setattr(
        web,
        "load_output_topology",
        lambda: pytest.fail("blocked summed capture must not inspect the graph"),
    )
    monkeypatch.setattr(
        web,
        "load_measurement_state",
        lambda _topology: pytest.fail("blocked summed capture must not read evidence"),
    )
    monkeypatch.setattr(
        web,
        "load_safe_playback_state",
        lambda: pytest.fail("blocked summed capture must not arm playback"),
    )
    monkeypatch.setattr(
        web,
        "arm_safe_playback_session",
        lambda report: armed.setdefault("report", report) or {"status": "armed"},
    )

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert armed == {}
    assert payload["status"] == "refused"
    assert payload["reason"] == "active_summed_persisted_admission_unavailable"
    assert payload["audio_emitted"] is False


def test_summed_capture_refuses_unloaded_reverse_or_delay_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    monkeypatch.setattr(
        web,
        "load_output_topology",
        lambda: (_ for _ in ()).throw(AssertionError("must refuse before load")),
    )

    for candidate in (
        {"expect_null": True, "polarity": "invert_tweeter"},
        {"delay_ms": 0.1, "delay_target_role": "tweeter"},
    ):
        payload = asyncio.run(
            web.play_summed_capture_sweep(
                {"speaker_group_id": "mono", **candidate},
                camilla_factory=lambda: object(),
            )
        )
        assert payload["status"] == "refused"
        assert payload["reason"] == "active_summed_persisted_admission_unavailable"
        assert payload["audio_emitted"] is False


def test_resilient_restore_does_not_retry_cancelled_child(monkeypatch):
    # The wait now lives in restore_wait, so a caller that only needs to put a
    # graph back does not import this module's commissioning stack; this module
    # still consumes it as `_resilient`.
    from jasper.active_speaker import restore_wait

    shield_calls = 0

    async def fake_shield(_task):
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls > 1:
            raise AssertionError("cancelled cleanup task was retried")
        raise asyncio.CancelledError

    class CancelledTask:
        def cancelled(self):
            return True

    monkeypatch.setattr(restore_wait.asyncio, "shield", fake_shield)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restore_wait.await_restore_task_resilient(CancelledTask()))
    assert shield_calls == 1


def test_summed_test_playback_does_not_block_the_correction_loop(monkeypatch):
    """C4a-6: the summed-test stimulus must play OFF the shared correction loop.

    The crossover summed test previously ran ``aplay`` via a synchronous
    ``subprocess.run`` directly on the single background correction loop
    (``jasper-correction-loop``), stalling every other correction/commissioning
    request — status polls, SSE progress, the safe-playback TTL deadman — for
    the whole stimulus duration.

    This pins the fix behaviourally: while playback is "in flight", a concurrent
    coroutine scheduled on the same loop must keep making progress. We stand in
    for the real ``aplay`` two ways at once: the off-loop primitive
    (``play_sweep``) yields via ``await asyncio.sleep``, while the old blocking
    primitive (``subprocess.run``) would ``time.sleep`` and freeze the loop
    thread. Reverting to ``subprocess.run`` makes the ticker starve and the
    assertion fail (mutation check).
    """

    playback_seconds = 0.30

    async def _fake_play_sweep(wav_path, *, alsa_device, timeout_s):
        # Off-loop: yields control so the loop can run other coroutines.
        await asyncio.sleep(playback_seconds)

    class _CompletedProc:
        returncode = 0
        stderr = ""

    class _BlockingRun:
        """Stand-in for the removed blocking ``subprocess.run`` path.

        If the code under test ever calls ``subprocess.run`` again it freezes
        the loop thread for the playback duration — exactly the bug. It returns
        a clean completed-process so the regression manifests as loop starvation
        (the ``ticks`` assertion below), not as an exception.
        """

        def __call__(self, *args, **kwargs):
            time.sleep(playback_seconds)
            return _CompletedProc()

    monkeypatch.setattr(correction_playback, "play_sweep", _fake_play_sweep)
    monkeypatch.setattr(web.subprocess, "run", _BlockingRun())

    # ``start_tone_playback`` is lazily imported inside the function, so patch
    # it on its source module.
    monkeypatch.setattr(
        active_playback,
        "start_tone_playback",
        lambda *a, **k: {"status": "completed", "tone": {"level_dbfs": -72.0}},
    )
    monkeypatch.setattr(
        web,
        "_combined_speech_stimulus_wav_path",
        lambda: ("/tmp/jts-fake-summed-stimulus.wav", {"duration_s": playback_seconds}),
    )

    async def _fake_load(**kwargs):
        return {"load": {"status": "loaded"}}

    async def _fake_rollback(**kwargs):
        return {"status": "rolled_back"}

    monkeypatch.setattr(web, "_load_summed_commissioning_config", _fake_load)
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", _fake_rollback)
    monkeypatch.setattr(web, "_commission_tone_select_fanin_lane", lambda: {"status": "ok"})
    monkeypatch.setattr(
        web,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: {"status": "ok", "reason": reason},
    )

    async def _scenario():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            # Tick frequently relative to the playback window. A responsive loop
            # accumulates many ticks during the ~0.30 s "playback".
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        playback = await web._play_summed_commission_tone(
            {},
            safe_session={"status": "armed"},
            topology=object(),
            speaker_group_id="mono",
            startup_gate_calibration_level=None,
            preset=object(),
            crossover_preview=None,
            camilla_factory=lambda: object(),
        )
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        return playback, ticks

    playback, ticks = asyncio.run(_scenario())

    # Playback completed through the off-loop primitive...
    assert playback["status"] == "completed"
    assert playback["backend"] == web.SUMMED_COMMISSION_SPEECH_BACKEND
    assert playback["audio_emitted"] is True
    # ...and the loop stayed responsive: many ticks landed during playback.
    # A blocked loop would yield ~0-1 ticks; require clearly more.
    assert ticks >= 5, f"correction loop appears blocked during playback (ticks={ticks})"


def test_summed_test_playback_dispatches_off_loop_primitive():
    """Structural mutation guard: no synchronous ``subprocess.run`` on the loop.

    Complements the behavioural test. ``_play_summed_commission_tone`` must
    dispatch the stimulus through the async off-loop primitive (``play_sweep``,
    which uses ``asyncio.create_subprocess_exec``) and must not reintroduce a
    blocking ``subprocess.run`` / ``subprocess.call`` / ``subprocess.Popen(...).wait``
    in the playback path.
    """

    src = web.__loader__.get_source(web.__name__)
    assert src is not None
    func_start = src.index("async def _play_summed_commission_tone(")
    func_end = src.index("async def start_summed_test(", func_start)
    body = src[func_start:func_end]

    assert "await play_sweep(" in body, (
        "_play_summed_commission_tone must await play_sweep (off-loop aplay)"
    )
    assert "subprocess.run(" not in body, (
        "_play_summed_commission_tone reintroduced a blocking subprocess.run "
        "on the correction loop (C4a-6 regression)"
    )


def test_regenerate_crossover_preview_matches_sound_setups_preview_button(
    monkeypatch, tmp_path,
):
    """W6.11: the v2 flow's session-start preview ensure
    (``jasper.web.correction_crossover_v2.ensure_crossover_preview_ready``)
    calls :func:`web.regenerate_crossover_preview_from_current_draft` instead
    of reimplementing ``/sound/``'s Preview-button generation. Pin that it
    really is the SAME machinery: seeded against the same design
    draft/topology, its output matches
    ``jasper.web.sound_setup``'s own
    ``_active_speaker_crossover_preview_save_payload()`` byte-for-byte except
    for the wall-clock ``created_at``/``updated_at`` timestamps."""
    import json

    from jasper.output_topology import save_output_topology
    from jasper.web import sound_setup

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
        str(tmp_path / "via_web_commissioning.json"),
    )
    via_new = web.regenerate_crossover_preview_from_current_draft()

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(tmp_path / "via_sound_setup.json"),
    )
    via_sound = sound_setup._active_speaker_crossover_preview_save_payload()

    assert via_new["status"] == "ready_for_protected_staging"
    ignored = {"path", "created_at", "updated_at"}
    assert {k: v for k, v in via_new.items() if k not in ignored} == {
        k: v for k, v in via_sound.items() if k not in ignored
    }
