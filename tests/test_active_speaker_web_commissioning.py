# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free guards for secure active-speaker web measurement orchestration."""

from __future__ import annotations

import asyncio
import inspect

import pytest

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
                "startup_muted": bool(channel.startup_muted),
                "protection_required": bool(channel.protection_required),
                "protection_status": channel.protection_status,
            }
            for group in topology.speaker_groups
            for channel in group.channels
        ],
    }


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


def test_stage_startup_config_does_not_reread_mutable_preview_for_explicit_preset(
    monkeypatch,
):
    from jasper.active_speaker import crossover_preview, design_draft

    frozen_preset = object()
    stage_call = {}
    monkeypatch.setattr(
        crossover_preview,
        "build_crossover_preview",
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
    blocked_preview = {"status": "blocked"}
    stage_call = {}
    monkeypatch.setattr(design_draft, "load_design_draft", lambda: draft)
    monkeypatch.setattr(
        crossover_preview,
        "build_crossover_preview",
        lambda current_design_draft: (
            blocked_preview
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
    assert stage_call["crossover_preview"] is blocked_preview


def test_startup_anchor_stages_the_callers_resolved_source(monkeypatch):
    topology = _topology()
    frozen_preset = object()
    stage_call = {}
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
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
    """#2184: staging can fail for ~8 distinct reasons (blocked preview,
    active_playback_device_required, subwoofer_staging_unresolved, ...), each
    already carrying its own code+message via ``_issue`` inside
    ``stage_protected_startup_config``. The failure card must name that real
    cause, not the one generic ``commission_startup_anchor_not_staged`` code
    for all of them.
    """
    topology = _topology()
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
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


def test_automatic_measurement_source_peak_is_one_shared_default():
    from jasper.active_speaker import driver_acoustics
    from jasper.audio_measurement.sweep import synchronized_swept_sine

    sweep_default = inspect.signature(synchronized_swept_sine).parameters[
        "amplitude_dbfs"
    ].default
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert sweep_default == AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    assert driver_acoustics.DEFAULT_AMPLITUDE_DBFS == sweep_default


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
