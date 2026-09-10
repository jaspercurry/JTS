# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

import jasper.active_speaker.playback as active_playback
import jasper.audio_measurement.playback as measurement_playback
from jasper.active_speaker import web_commissioning as web
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from tests.active_speaker_fixtures import mono_output_topology


def _topology(**kwargs):
    return mono_output_topology(topology_name="Bench mono", **kwargs)


def _staged_anchor_for(topology, staged_path):
    """A staged-anchor stub in the shape ``staging.py`` really writes.

    The `topology` and `hardware` blocks are NOT decoration: since #2285
    /sound/ gates "already loaded" on
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


def test_async_commission_tone_select_cancellation_settles_and_releases_gate(
    monkeypatch,
):
    """Cancellation cannot orphan a late successful mux TEST_SELECT.

    ``asyncio.to_thread`` cannot stop its worker. Model the mux committing the
    selection only after caller cancellation, then cancel the caller again
    while release/restore is blocked. Cancellation must not propagate until
    the same owner has given the gate back.
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
            web._commission_tone_select_fanin_lane_async()
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

    assert calls == [
        "TEST_SELECT correction active-speaker-commissioning",
        "TEST_RELEASE active-speaker-commissioning",
    ]


def test_a_path_match_with_a_stale_topology_is_not_already_loaded(monkeypatch):
    """#2285: the anchor is gated on TWO terms, not on the config path alone.

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


def test_startup_anchor_forwards_the_specific_stage_failure_code(monkeypatch):
    """#2184: staging can fail for ~8 distinct reasons (stale preview,
    active_playback_device_required, subwoofer_staging_unresolved, ...), each
    already carrying its own code+message via ``_issue`` inside
    ``stage_protected_startup_config``. The failure card must name that real
    cause, not the one generic ``commission_startup_anchor_not_staged`` code
    for all of them.
    """
    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web, "ensure_missing_software_guards", lambda: (topology, False),
    )
    specific_issue = {
        "severity": "blocker",
        "code": "active_playback_device_required",
        "message": "no active playback device is assigned",
    }
    monkeypatch.setattr(
        web,
        "_stage_startup_config",
        lambda *a, **kw: {"status": "blocked", "issues": [specific_issue]},
    )

    result = asyncio.run(
        web._ensure_commission_startup_anchor(
            group="mono",
            role="woofer",
            staged_config={"status": "blocked"},
            current_config_path="/var/lib/camilladsp/configs/sound_current.yml",
            camilla_factory=lambda: object(),
            preset=object(),
            crossover_preview=None,
        )
    )

    assert result["load"]["issues"] == [specific_issue]


def test_startup_anchor_forwards_every_stage_blocker_not_only_the_first(monkeypatch):
    """#2184 follow-up: a stage can fail more than one gate at once. Every
    blocker must reach the household, headline (first) blocker still first,
    rather than dropping the rest on the floor."""
    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web, "ensure_missing_software_guards", lambda: (topology, False),
    )
    first_issue = {
        "severity": "blocker",
        "code": "active_playback_device_required",
        "message": "no active playback device is assigned",
    }
    second_issue = {
        "severity": "blocker",
        "code": "subwoofer_staging_unresolved",
        "message": "the subwoofer staging preset could not be resolved",
    }
    monkeypatch.setattr(
        web,
        "_stage_startup_config",
        lambda *a, **kw: {
            "status": "blocked",
            "issues": [first_issue, second_issue],
        },
    )

    result = asyncio.run(
        web._ensure_commission_startup_anchor(
            group="mono",
            role="woofer",
            staged_config={"status": "blocked"},
            current_config_path="/var/lib/camilladsp/configs/sound_current.yml",
            camilla_factory=lambda: object(),
            preset=object(),
            crossover_preview=None,
        )
    )

    assert result["load"]["issues"] == [first_issue, second_issue]


def test_startup_anchor_falls_back_to_the_generic_code_with_no_stage_issue(
    monkeypatch,
):
    """The fallback stays honest, never invented: a stage failure that
    reported no issue at all (an older build, a seam that forgot to mint
    one) still reaches the household as the generic
    ``commission_startup_anchor_not_staged`` code rather than a KeyError or a
    fabricated cause."""
    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web, "ensure_missing_software_guards", lambda: (topology, False),
    )
    monkeypatch.setattr(
        web, "_stage_startup_config", lambda *a, **kw: {"status": "blocked"},
    )

    result = asyncio.run(
        web._ensure_commission_startup_anchor(
            group="mono",
            role="woofer",
            staged_config={"status": "blocked"},
            current_config_path="/var/lib/camilladsp/configs/sound_current.yml",
            camilla_factory=lambda: object(),
            preset=object(),
            crossover_preview=None,
        )
    )

    assert result["load"]["issues"][0]["code"] == "commission_startup_anchor_not_staged"


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


def test_automatic_measurement_source_peak_is_one_shared_default():
    from jasper.active_speaker import driver_acoustics
    from jasper.audio_measurement.sweep import synchronized_swept_sine

    sweep_default = inspect.signature(synchronized_swept_sine).parameters[
        "amplitude_dbfs"
    ].default
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert sweep_default == AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    assert driver_acoustics.DEFAULT_AMPLITUDE_DBFS == sweep_default


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
    (``play_wav``) yields via ``await asyncio.sleep``, while the old blocking
    primitive (``subprocess.run``) would ``time.sleep`` and freeze the loop
    thread. Reverting to ``subprocess.run`` makes the ticker starve and the
    assertion fail (mutation check).
    """

    playback_seconds = 0.30

    async def _fake_play_wav(wav_path, *, alsa_device, timeout_s):
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

    monkeypatch.setattr(measurement_playback, "play_wav", _fake_play_wav)
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


def test_regenerate_crossover_preview_matches_sound_setups_preview_button(
    monkeypatch, tmp_path,
):
    """W6.11: the v2 flow's session-start preview ensure
    (``jasper.web.correction_crossover_v2.ensure_crossover_preview_ready``)
    calls :func:`web.regenerate_crossover_preview_from_current_draft` instead
    of reimplementing ``/sound/``'s Preview-button generation. Pin that it
    really is the SAME machinery: seeded against the same design
    draft/topology, its output matches
    ``jasper.web.sound_active_speaker``'s own
    ``_active_speaker_crossover_preview_save_payload()`` byte-for-byte except
    for the wall-clock ``created_at``/``updated_at`` timestamps."""
    import json

    from jasper.output_topology import save_output_topology
    from jasper.web import sound_active_speaker

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
    via_sound = sound_active_speaker._active_speaker_crossover_preview_save_payload()

    assert via_new["status"] == "ready_for_protected_staging"
    ignored = {"path", "created_at", "updated_at"}
    assert {k: v for k, v in via_new.items() if k not in ignored} == {
        k: v for k, v in via_sound.items() if k not in ignored
    }
