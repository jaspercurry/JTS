# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

import jasper.active_speaker.playback as active_playback
import jasper.correction.playback as correction_playback
from jasper.active_speaker import web_commissioning as web
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from tests.test_active_speaker_measurement import _topology
from tests.test_active_speaker_profile import _two_way_preset


def test_driver_capture_sweep_requires_confirmed_driver(monkeypatch):
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: {})

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_required"


def test_driver_capture_sweep_refuses_expired_floor_confirmation(monkeypatch):
    measurements = {
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": {
                    "captured": True,
                    "playback_id": "play-woofer",
                    "test_level_dbfs": -72.0,
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "_load_driver_commissioning_config_for_level",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not load")),
    )

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "refused"
    assert payload["reason"] == "driver_floor_confirmation_expired"


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
    summed = web.automatic_summed_excitation(topology, profile)

    assert driver["status"] == "blocked"
    assert driver["reason"] == reason
    assert summed["status"] == "blocked"
    assert summed["reason"] == reason


@pytest.mark.parametrize("legacy_floor_dbfs", [-20.0, -60.0])
def test_driver_capture_sweep_never_reuses_legacy_floor_level(
    monkeypatch, legacy_floor_dbfs
):
    topology = _topology()
    measurements = {
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": {
                    "captured": True,
                    "playback_id": "play-woofer",
                    "test_level_dbfs": legacy_floor_dbfs,
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: _applied_excitation_profile(topology=topology, gain_db=-9.0),
    )
    load_call = {}

    async def fake_load(**kwargs):
        load_call.update(kwargs)
        return {"load": {"status": "loaded"}}

    play_call = {}

    async def fake_play(**kwargs):
        play_call.update(kwargs)
        excitation = {
            key: value
            for key, value in kwargs["planned_excitation"].items()
            if key != "status"
        }
        return {
            "status": "completed",
            "audio_emitted": True,
            "playback_id": "play-woofer",
            "sweep_meta": {
                "amplitude_dbfs": AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
            },
            "excitation": excitation,
        }

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", fake_load)
    monkeypatch.setattr(web, "_play_capture_sweep", fake_play)

    payload = asyncio.run(
        web.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "woofer"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "completed"
    assert payload["test_level_dbfs"] == -9.0
    assert load_call["level_dbfs"] == -9.0
    assert play_call["level_dbfs"] == -9.0
    assert play_call["planned_excitation"]["gain_source"] == (
        web.AUTOMATIC_EXCITATION_GAIN_SOURCE
    )
    assert payload["test_level_dbfs"] != legacy_floor_dbfs


def test_driver_capture_sweep_refuses_before_loading_when_applied_gain_is_stale(
    monkeypatch,
):
    topology = _topology()
    measurements = {
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": {
                    "captured": True,
                    "playback_id": "play-woofer",
                    "test_level_dbfs": -60.0,
                    "issues": [],
                },
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
        )
    )

    assert payload["status"] == "refused"
    assert payload["audio_emitted"] is False
    assert payload["reason"] == "active_applied_profile_snapshot_topology_stale"


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


def test_summed_capture_sweep_arms_safe_session_for_mutual_exclusion(monkeypatch):
    armed = {}
    measurements = {
        "summary": {
            "latest_summed_tests": {
                "mono": {
                    "captured": True,
                    "audio_emitted": True,
                    "summed_test_id": "sum-1",
                    "tone": {"level_dbfs": -72.0},
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "idle"})
    monkeypatch.setattr(
        web,
        "arm_safe_playback_session",
        lambda report: armed.setdefault("report", report) or {"status": "armed"},
    )
    async def fake_load(**kwargs):
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "test_block",
                    "message": "blocked in test",
                }],
            },
        }

    monkeypatch.setattr(web, "_load_applied_summed_measurement_config", fake_load)

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert armed["report"]["status"] == "ready"
    assert payload["status"] == "blocked"
    assert payload["reason"] == "summed_capture_sweep_load_failed"


def _full_applied_profile(*, topology=None, topology_id=None):
    profile = _applied_excitation_profile(
        topology=topology,
        topology_id=topology_id,
    )
    profile["baseline_id"] = "baseline-full"
    return profile


def test_summed_capture_ignores_legacy_minus_80_level_and_uses_applied_graph(
    monkeypatch,
):
    topology = _topology()
    measurements = {
        "summary": {
            "latest_summed_tests": {
                "mono": {
                    "captured": True,
                    "audio_emitted": True,
                    "summed_test_id": "sum-legacy",
                    "tone": {"level_dbfs": -80.8},
                    "issues": [],
                },
            },
        },
    }
    excitation = web.automatic_summed_excitation(
        topology,
        _full_applied_profile(topology=topology),
    )
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})

    async def fake_load(**_kwargs):
        return {
            "load": {
                "status": "loaded",
                "previous_config_path": "/tmp/normal.yml",
            },
            "excitation": excitation,
        }

    monkeypatch.setattr(web, "_load_applied_summed_measurement_config", fake_load)
    play_call = {}

    async def fake_play(**kwargs):
        play_call.update(kwargs)
        return {
            "status": "completed",
            "audio_emitted": True,
            "playback_id": "sum-legacy",
            "sweep_meta": {"amplitude_dbfs": -12.0},
            "excitation": {
                key: value
                for key, value in excitation.items()
                if key != "status"
            },
        }

    monkeypatch.setattr(web, "_play_capture_sweep", fake_play)

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "completed"
    assert payload["test_level_dbfs"] == 0.0
    assert payload["test_level_dbfs"] != -80.8
    assert play_call["level_dbfs"] == 0.0
    assert play_call["planned_excitation"]["scope"] == (
        "sweep_plus_applied_full_layer_a_graph"
    )
    assert play_call["planned_excitation"]["corrections"]["woofer"] == {
        "gain_db": -9.0,
        "delay_ms": 0.25,
        "inverted": False,
        "effective_peak_dbfs": -21.0,
    }
    assert callable(play_call["rollback_capture_config"])


def test_summed_capture_refuses_stale_applied_snapshot_before_audio(monkeypatch):
    topology = _topology()
    measurements = {
        "summary": {
            "latest_summed_tests": {
                "mono": {
                    "captured": True,
                    "audio_emitted": True,
                    "summed_test_id": "sum-legacy",
                    "tone": {"level_dbfs": -80.8},
                    "issues": [],
                },
            },
        },
    }
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda _topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})

    async def blocked_load(**_kwargs):
        return {
            "load": {
                "status": "blocked",
                "issues": [{
                    "severity": "blocker",
                    "code": "applied_baseline_snapshot_topology_stale",
                    "message": "the applied crossover belongs to another topology",
                }],
            },
        }

    monkeypatch.setattr(
        web,
        "_load_applied_summed_measurement_config",
        blocked_load,
    )
    monkeypatch.setattr(
        web,
        "_play_capture_sweep",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not play")),
    )

    payload = asyncio.run(
        web.play_summed_capture_sweep(
            {"speaker_group_id": "mono"},
            camilla_factory=lambda: object(),
        )
    )

    assert payload["status"] == "blocked"
    assert payload["audio_emitted"] is False
    assert payload["issues"][0]["code"] == (
        "applied_baseline_snapshot_topology_stale"
    )


def test_summed_measurement_loader_recomposes_validates_and_loads_snapshot(
    monkeypatch, tmp_path
):
    from jasper.active_speaker import baseline_profile
    from jasper import dsp_apply

    topology = _topology()
    applied = _full_applied_profile(topology=topology)
    target = tmp_path / "summed.yml"
    monkeypatch.setenv(web.AUTOMATIC_SUMMED_CONFIG_PATH_ENV, str(target))
    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: applied,
    )
    recompose_call = {}

    def recompose(_topology, *, applied_profile, out_path):
        recompose_call.update(
            topology=_topology,
            applied_profile=applied_profile,
            out_path=out_path,
        )
        return "pipeline: {}\n", []

    monkeypatch.setattr(baseline_profile, "recompose_applied_baseline_yaml", recompose)
    monkeypatch.setattr(
        dsp_apply,
        "validate_camilla_config",
        lambda path: SimpleNamespace(
            ok_to_apply=True,
            to_dict=lambda: {"status": "valid", "path": str(path)},
        ),
    )
    loaded_paths = []

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            assert best_effort is False
            return "/tmp/normal.yml"

        async def set_config_file_path(self, path, *, best_effort):
            assert best_effort is False
            loaded_paths.append(path)
            return True

    payload = asyncio.run(
        web._load_applied_summed_measurement_config(
            topology=topology,
            camilla_factory=Cam,
        )
    )

    assert payload["load"]["status"] == "loaded"
    assert payload["load"]["previous_config_path"] == "/tmp/normal.yml"
    assert payload["excitation"]["baseline_id"] == "baseline-full"
    assert recompose_call == {
        "topology": topology,
        "applied_profile": applied,
        "out_path": target,
    }
    assert loaded_paths == [str(target)]


@pytest.mark.parametrize(
    ("load_outcome", "rollback_fails"),
    [
        ("false", False),
        ("exception", False),
        ("false", True),
        ("exception", True),
    ],
)
def test_summed_measurement_loader_restores_every_unsuccessful_load(
    monkeypatch,
    tmp_path,
    load_outcome,
    rollback_fails,
):
    from jasper import dsp_apply
    from jasper.active_speaker import baseline_profile

    topology = _topology()
    applied = _full_applied_profile(topology=topology)
    target = tmp_path / "summed.yml"
    previous = "/tmp/normal.yml"
    monkeypatch.setenv(web.AUTOMATIC_SUMMED_CONFIG_PATH_ENV, str(target))
    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: applied,
    )
    monkeypatch.setattr(
        baseline_profile,
        "recompose_applied_baseline_yaml",
        lambda *_args, **_kwargs: ("pipeline: {}\n", []),
    )
    monkeypatch.setattr(
        dsp_apply,
        "validate_camilla_config",
        lambda path: SimpleNamespace(
            ok_to_apply=True,
            to_dict=lambda: {"status": "valid", "path": str(path)},
        ),
    )
    log_calls = []
    monkeypatch.setattr(
        web,
        "log_event",
        lambda *args, **kwargs: log_calls.append((args, kwargs)),
    )
    calls = []

    class Cam:
        async def get_config_file_path(self, *, best_effort):
            return previous

        async def set_config_file_path(self, path, *, best_effort):
            calls.append(path)
            if path == str(target):
                if load_outcome == "exception":
                    raise RuntimeError("transient load failed")
                return False
            if rollback_fails:
                raise RuntimeError("rollback failed")
            return True

    payload = asyncio.run(
        web._load_applied_summed_measurement_config(
            topology=topology,
            camilla_factory=Cam,
        )
    )

    assert payload["status"] == "blocked"
    assert calls == [str(target), previous]
    assert payload["rollback"]["status"] == (
        "failed" if rollback_fails else "rolled_back"
    )
    if rollback_fails:
        assert "rollback failed" in payload["rollback"]["error"]
        assert [issue["code"] for issue in payload["load"]["issues"]] == [
            "automatic_summed_config_rollback_failed",
            "automatic_summed_config_load_failed",
        ]
        assert len(log_calls) == 1
        args, kwargs = log_calls[0]
        assert args[1] == "active_speaker.automatic_summed_config_rollback"
        assert kwargs["level"] == web.logging.WARNING
        assert kwargs["status"] == "failed"
        assert kwargs["failure_mode"] == (
            "load_exception"
            if load_outcome == "exception"
            else "load_returned_false"
        )
    else:
        assert [issue["code"] for issue in payload["load"]["issues"]] == [
            "automatic_summed_config_load_failed"
        ]
        assert log_calls == []


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
