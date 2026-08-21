# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP coupling."""

from __future__ import annotations

from pathlib import Path

import pytest

# The conf.d this repo actually installs — the honest "box on the shipped wire"
# fixture for the gates that read both PCM blocks.
SHIPPED_RING_CONF_D = (
    Path(__file__).resolve().parents[1] / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)

from jasper.env_file import read_value
from jasper.fanin.coupling_reconcile import (
    FANIN_ENV_PATH,
    OUTPUTD_ENV_PATH,
    _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV,
    _outputd_actions,
    default_ring_gates,
    read_persisted_coupling,
    reconcile_auto,
    reconcile_coupling,
    ring_edge_width_ready,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_RING_PATH_ENV_VAR,
    OUTPUTD_RING_SLOTS_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """Fixture wrapper over :func:`isolate_base_jasper_env` (autouse here)."""
    isolate_base_jasper_env(tmp_path, monkeypatch)


def isolate_base_jasper_env(tmp_path, monkeypatch):
    """Keep effective-env tests independent of the developer host's /etc state."""
    # Ring-arm mechanics below need an explicit, commissioned passive stereo
    # speaker. Empty speaker_groups now means "unconfigured and silent", so it
    # is not a neutral default for tests that are specifically about the later
    # asset, geometry, and daemon-order gates.
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )

    jasper_env = tmp_path / "jasper.env"
    jasper_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )
    # ...and of its /var/lib state. ``resolve_ring_wire`` reads the box's declared
    # ring wire off the SAME jasper.env -> fanin.env chain jasper-fanin resolves,
    # so a real /var/lib/jasper/fanin.env on the host running the suite (a Pi, or
    # a dev laptop that ever ran the installer) would reach these tests. The
    # module-level constant is what that read consults; every test here that
    # exercises a real fanin.env passes its own path explicitly.
    fanin_env = tmp_path / "isolated-fanin.env"
    fanin_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    # Keep every main() invocation's entry flock inside the test tmp dir — never
    # the real /run path — so parallel test workers can't contend on one file.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.ENTRY_LOCK_PATH",
        str(tmp_path / "entry.lock"),
    )
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(
        OutputTopology.from_mapping(
            {
                "artifact_schema_version": 1,
                "kind": OUTPUT_TOPOLOGY_KIND,
                "topology_id": "test-passive-stereo",
                "name": "Test passive stereo",
                "status": "draft",
                "hardware": {
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple USB-C audio adapter",
                    "physical_output_count": 2,
                },
                "speaker_groups": [
                    {
                        "id": "left",
                        "label": "Left",
                        "kind": "left",
                        "mode": "full_range_passive",
                        "channels": [
                            {"role": "full_range", "physical_output_index": 0}
                        ],
                    },
                    {
                        "id": "right",
                        "label": "Right",
                        "kind": "right",
                        "mode": "full_range_passive",
                        "channels": [
                            {"role": "full_range", "physical_output_index": 1}
                        ],
                    },
                ],
                "routing": {
                    "main_left_group_id": "left",
                    "main_right_group_id": "right",
                },
            }
        ),
        path=topology_path,
    )
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))


def _recorder(
    *,
    outputd_ok=True,
    fanin_ok=True,
    camilla_ok=True,
    camilla_fail_for=None,
):
    """Build (calls, restart_outputd, restart_fanin, reconcile_camilla) hooks."""
    calls: list[str] = []

    def restart_outputd() -> tuple[bool, str]:
        calls.append("outputd")
        return (outputd_ok, "" if outputd_ok else "outputd restart failed")

    def restart_fanin() -> tuple[bool, str]:
        calls.append("fanin")
        return (fanin_ok, "" if fanin_ok else "fanin restart failed")

    def reconcile_camilla(coupling: str) -> tuple[bool, str]:
        calls.append(f"camilla:{coupling}")
        ok = camilla_ok and (camilla_fail_for is None or coupling != camilla_fail_for)
        return (ok, "reconciled" if ok else "invalid config")

    return calls, restart_outputd, restart_fanin, reconcile_camilla


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _reconcile(
    desired: str | None,
    *,
    fanin_env: Path,
    outputd_env: Path,
    restart_outputd,
    restart_fanin,
    reconcile_camilla,
    **kwargs,
):
    # Keep tests hermetic: never let the disarm path's default hardware-reconcile
    # kick reach the real restart broker. Kick-behaviour tests inject their own.
    kwargs.setdefault("kick_hardware_reconcile", lambda: (True, ""))
    # Same for the assistant-width voice restart: it fires only on a width
    # TRANSITION, so most tests never reach it, but a test that flips a
    # declared-wide box's coupling would otherwise drive the real broker.
    kwargs.setdefault("restart_voice", lambda: (True, ""))
    return reconcile_coupling(
        desired,
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_outputd=restart_outputd,
        restart_fanin=restart_fanin,
        reconcile_camilla=reconcile_camilla,
        **kwargs,
    )


def test_coupling_reconciler_gets_env_action_from_runtime_plan():
    import jasper.fanin.coupling_reconcile as cr

    source = Path(cr.__file__).read_text(encoding="utf-8")

    assert "fanin_coupling_action" in source


def test_disarm_camilla_failure_still_restarts_fanin_and_outputd(tmp_path):
    # Disarm FROM a shm_ring-armed box: even when the camilla reconcile to loopback
    # fails, the disarm still restarts fan-in + outputd (never leave them stranded
    # on the ring).
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n",
    )
    calls, ro, rf, rc = _recorder(camilla_fail_for="loopback")

    res = _reconcile(
        "loopback",
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert calls == ["camilla:loopback", "fanin", "outputd"]
    assert res.ok is False
    assert res.restarted_fanin is True
    assert res.restarted_outputd is True


def test_old_fifo_literal_failsafe_to_loopback(tmp_path):
    # A removed/unknown literal (the old "fifo", or the deleted transport_pipe)
    # persisted in fanin.env fails safe to a loopback disarm.
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"JASPER_OUTPUTD_SINK=dual_apple\n{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        "fifo",
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.desired == "loopback" and res.direction == "disarm"
    assert calls == ["camilla:loopback", "fanin", "outputd"]
    assert read_persisted_coupling(fanin_env) == "loopback"
    # The stale ring bridge key is cleared; the operator's own line survives.
    outputd_text = outputd_env.read_text()
    assert read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_text


def test_no_apply_shm_ring_is_arm_direction(tmp_path):
    # NIT 7: a --no-apply shm_ring write is an ARM (was mislabeled "disarm" because
    # the check only compared against transport_pipe).
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert res.ok and res.changed and calls == []
    assert res.direction == "arm"
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_no_apply_loopback_is_disarm_direction(tmp_path):
    # The complement: a --no-apply loopback write is a disarm.
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.changed and calls == []
    assert res.direction == "disarm"


def test_arm_preserves_coexisting_keys_and_custom_outputd_ring_path(
    tmp_path, _ring_assets_present
):
    fanin_env = _write(
        tmp_path / "fanin.env",
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536\n# operator note\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        f"{OUTPUTD_RING_PATH_ENV_VAR}=/run/custom/content.ring\n",
    )
    _, ro, rf, rc = _recorder()

    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    fanin_body = fanin_env.read_text(encoding="utf-8")
    outputd_body = outputd_env.read_text(encoding="utf-8")
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536" in fanin_body
    assert "# operator note" in fanin_body
    assert "JASPER_CAMILLA_CHUNKSIZE=256" in outputd_body
    # The shm_ring arm preserves the operator's custom Ring B path.
    assert (
        read_value(outputd_body, OUTPUTD_RING_PATH_ENV_VAR)
        == "/run/custom/content.ring"
    )


def test_env_write_failure_aborts_before_daemon_ops(tmp_path, monkeypatch):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("jasper.fanin.coupling_reconcile.atomic_write_text", boom)
    res = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert res.ok is False and res.direction == "error" and calls == []


def test_read_persisted_coupling_defaults_loopback(tmp_path):
    assert read_persisted_coupling(tmp_path / "absent.env") == "loopback"
    assert read_persisted_coupling(_write(tmp_path / "f.env", "X=1\n")) == "loopback"


def test_default_env_paths_are_reconciler_owned_envs():
    assert FANIN_ENV_PATH == "/var/lib/jasper/fanin.env"
    assert OUTPUTD_ENV_PATH == "/var/lib/jasper/outputd.env"


def test_cli_main_hydrates_env_files_before_reconciling(monkeypatch):
    from jasper.fanin import coupling_reconcile as cr

    order: list[str] = []
    monkeypatch.setattr(
        "jasper.env_load.load_env_files", lambda *a, **k: order.append("hydrate")
    )

    def fake_reconcile(*a, **k):
        order.append("reconcile")
        return cr.CouplingResult(
            ok=True,
            desired="loopback",
            changed=False,
            direction="confirm",
        )

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main(["loopback"])
    assert rc == 0
    assert order == ["hydrate", "reconcile"]


def test_cli_main_configures_info_logging(monkeypatch, capsys):
    """main() must install an INFO-capable root handler: the #1233 camilla
    pause/resume evidence and auto_resolved transition are INFO-level ``event=``
    lines, and without basicConfig the root logger
    falls back to Python's lastResort handler (WARNING+), silently dropping
    them from the oneshot units' journals (observed on jts.local build
    41886ab8, 2026-07-11)."""
    import logging

    from jasper.fanin import coupling_reconcile as cr
    from jasper.log_event import log_event

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(
        cr,
        "reconcile_coupling",
        lambda *a, **k: cr.CouplingResult(
            ok=True, desired="loopback", changed=False, direction="confirm"
        ),
    )
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    # Simulate the fresh CLI interpreter: no root handler yet. (Under pytest the
    # capture plugin's own root handlers would make basicConfig a silent no-op.)
    root.handlers.clear()
    try:
        assert cr.main(["loopback"]) == 0
        assert root.handlers, "main() must configure a root handler (basicConfig)"
        assert root.getEffectiveLevel() <= logging.INFO
        # The exact line that was silently dropped pre-fix now reaches a handler.
        log_event(
            cr.logger,
            "fanin.coupling_reconcile",
            result="camilla_paused_for_fanin_restart",
            reason="test",
        )
    finally:
        for h in root.handlers[:]:
            if h not in saved_handlers:
                root.removeHandler(h)
                h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    err = capsys.readouterr().err
    assert "event=fanin.coupling_reconcile" in err
    assert "result=camilla_paused_for_fanin_restart" in err


def test_cli_explicit_choice_stamps_operator_marker(tmp_path):
    """The explicit positional path writes JASPER_FANIN_COUPLING_CHOICE=operator so
    a later --auto pass treats this coupling as an operator choice (the revert
    lever)."""
    from jasper.fanin.coupling_auto import COUPLING_CHOICE_ENV_VAR

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        mark_operator_choice=True,
    )
    assert result.ok
    assert read_value(fanin_env.read_text(), COUPLING_CHOICE_ENV_VAR) == "operator"


def test_marker_only_change_does_not_bounce_daemons(tmp_path):
    """Stamping the operator marker on an already-loopback box must NOT trigger a
    disarm bounce — the coupling did not move, so it stays on the confirm path."""
    from jasper.fanin.coupling_auto import COUPLING_CHOICE_ENV_VAR

    fanin_env = _write(
        tmp_path / "fanin.env", "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        mark_operator_choice=True,
    )
    assert result.direction == "confirm"
    # No fan-in/outputd bounce — only the camilla confirm ran.
    assert "fanin" not in calls
    assert "outputd" not in calls
    # But the marker WAS written (env moved, confirm reports changed=True).
    assert read_value(fanin_env.read_text(), COUPLING_CHOICE_ENV_VAR) == "operator"
    assert result.changed is True


def test_production_confirm_uses_nonforcing_camilla_fast_path(monkeypatch):
    """Unchanged source passes may verify DSP drift, never force a reload."""

    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime

    observed = []

    async def fake_reconcile_current_dsp(**kwargs):
        observed.append(kwargs)
        return {"status": "unchanged"}

    monkeypatch.setattr(runtime, "reconcile_current_dsp", fake_reconcile_current_dsp)

    assert cr._reconcile_camilla(
        COUPLING_LOOPBACK,
        reason="source steady state",
        force=False,
    ) == (True, "unchanged")
    assert observed == [{"force": False, "coupling": COUPLING_LOOPBACK}]


def _rung_over_a_real_camilla_down_reconcile(box, tmp_path, monkeypatch, **rung_kwargs):
    """Run the REAL reconcile on a REAL camilla-down box, into the REAL rung.

    Nothing about the reconcile is canned: a real carrier, a real re-emit, a
    real statefile, and the real controller factory refusing the connection. The
    only redirection is the two paths, into ``tmp_path``. That matters because
    the defect this pins is a payload SHAPE crossing a module boundary — a
    hand-written payload could assert the shape I expect rather than the shape
    the reconcile actually produces.

    Returns ``(ok, detail, payload)``.
    """

    from jasper.sound.profile import SoundProfile, save_profile
    from jasper.sound.runtime import reconcile_current_dsp as real_reconcile

    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime
    from tests.test_reconcile_camilla_down import _DownCamilla

    _config, config_dir, _statefile = box
    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(), profile_path)

    captured: dict = {}

    async def redirected(**kwargs):
        payload = await real_reconcile(
            profile_path=profile_path,
            config_dir=config_dir,
            camilla_factory=lambda: _DownCamilla(),
            **kwargs,
        )
        captured.clear()
        captured.update(payload)
        return payload

    monkeypatch.setattr(runtime, "reconcile_current_dsp", redirected)
    ok, detail = cr._reconcile_camilla(cr.COUPLING_SHM_RING, **rung_kwargs)
    return ok, detail, captured


@pytest.mark.parametrize("box_name", ["flat_streambox", "roleful"])
@pytest.mark.parametrize(
    ("reason", "force"), [("confirm", False), ("arm", True)]
)
def test_a_camilla_down_reconcile_never_satisfies_the_camilla_rung(
    box_name, reason, force, tmp_path, monkeypatch
):
    """THE BLOCKER PIN: this rung's contract is "re-emit AND LOAD".

    Since #2664 the reconcile SUCCEEDS with CamillaDSP down — it converges the
    graph the box will boot, which is right for a deploy. This rung must not read
    that as a load. Before #2664 it was protected for free (the reconcile raised
    and the except turned it into a failure); the guard restores that outcome
    deliberately.

    Both box shapes are covered because they take DIFFERENT acceptance branches
    and the first fix only narrowed the third one: a commissioned roleful box and
    a flat box both answer ``reconciled`` and hit the common branch, so guarding
    only the anchor branch would have left the entire production fleet accepting
    a dead daemon while the mid-commission special case stayed protected.
    """

    from tests.test_reconcile_camilla_down import _flat_streambox, _roleful_box

    build = _flat_streambox if box_name == "flat_streambox" else _roleful_box
    box = build(tmp_path, monkeypatch)

    ok, detail, payload = _rung_over_a_real_camilla_down_reconcile(
        box, tmp_path, monkeypatch, reason=reason, force=force
    )

    # The reconcile really did succeed over the statefile — so this test is
    # exercising the acceptance branches, not some unrelated earlier failure.
    assert payload["transport"] == "statefile", payload
    assert payload["status"] in ("reconciled", "unchanged"), payload

    assert ok is False, (detail, payload)
    # Typed enough for an operator to act on: names the cause, not just "failed".
    assert "camilla down" in detail
    assert "statefile" in detail


def test_the_camilla_rung_answers_a_down_daemon_the_same_way_it_used_to(
    tmp_path, monkeypatch
):
    """The pre-#2664 control for the guard above.

    Before this PR a down daemon reached the rung as a RAISE, which the blanket
    except turned into ``(False, "camilla reconcile raised: …")``. The guard's
    whole claim is that it RESTORES that outcome rather than inventing one, so
    the old shape is asserted here beside the new one: both directions refuse,
    and only the wording differs.
    """

    from jasper.camilla import CamillaUnavailable
    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime

    async def raising_reconcile(**_kwargs):
        raise CamillaUnavailable("[Errno 111] Connection refused")

    monkeypatch.setattr(runtime, "reconcile_current_dsp", raising_reconcile)
    ok, detail = cr._reconcile_camilla(
        cr.COUPLING_SHM_RING, reason="confirm", force=False
    )

    assert ok is False
    assert "Connection refused" in detail


def _arm_reconcile_returning(monkeypatch, payload: dict):
    """Drive the arm ladder's camilla rung over a canned reconcile payload."""

    from jasper.fanin import coupling_reconcile as cr
    from jasper.sound import runtime

    async def fake_reconcile_current_dsp(**_kwargs):
        return payload

    monkeypatch.setattr(runtime, "reconcile_current_dsp", fake_reconcile_current_dsp)
    # The anchor really IS converged on disk in both directions, so the only
    # thing that can separate the two cases below is which reader answered.
    monkeypatch.setattr(
        cr, "ring_endpoint_anchor_converged", lambda **_k: (True, "anchor ok")
    )
    return cr._reconcile_camilla(
        cr.COUPLING_SHM_RING, reason="arm", force=True
    )


def _staged_anchor_skip(transport: str) -> dict:
    from jasper.fanin.coupling_reconcile import CARRIER_TRANSIENT_ACTIVE_REFUSAL

    return {
        "status": "skipped",
        "reason": CARRIER_TRANSIENT_ACTIVE_REFUSAL,
        "transport": transport,
        "current_config_path": "/var/lib/camilladsp/configs/"
        "active_speaker_staged_startup.yml",
    }


def test_the_arm_ladder_will_not_claim_a_converged_anchor_off_the_statefile(
    monkeypatch,
):
    """The consequence next door: ``transport`` gates the arm ladder's acceptance.

    ``jasper.fanin.coupling_reconcile._reconcile_camilla`` accepts ONE skipped
    reconcile in the arm direction — a mid-commission roleful box parked on its
    all-muted staged anchor — and proves convergence from
    ``payload["current_config_path"]``. Its own comment says why that is sound:
    the value is "the DAEMON's own answer … not the statefile's", because a
    statefile moved mid-arm would otherwise report a converged anchor while
    CamillaDSP still writes the lane the ring replaced (#2364).

    Since #2664 that payload can carry the STATEFILE's answer instead — exactly
    the weaker reader the acceptance excludes. Without a guard the arm ladder
    would report a converged anchor about a CamillaDSP that is not running.
    """

    ok, detail = _arm_reconcile_returning(
        monkeypatch, _staged_anchor_skip("statefile")
    )
    assert ok is False, detail


def test_the_arm_ladder_still_accepts_the_anchor_over_the_websocket(monkeypatch):
    """The positive control for the guard above.

    Same payload, same on-disk anchor, only the reader differs. Without this the
    guard could have been an unconditional refusal — which would break every
    mid-commission arm rather than only the one it is meant to exclude.
    """

    from jasper.fanin.coupling_reconcile import CAMILLA_ANCHOR_CONVERGED_DETAIL

    ok, detail = _arm_reconcile_returning(
        monkeypatch, _staged_anchor_skip("websocket")
    )
    assert ok is True, detail
    assert detail == CAMILLA_ANCHOR_CONVERGED_DETAIL


def test_cli_auto_dispatches_to_reconcile_auto(monkeypatch, capsys):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    seen = {}

    def fake_auto(*a, **k):
        seen.update(k)
        return cr.AutoResult(
            ok=True,
            owned=True,
            coupling="shm_ring",
            gadget_present=True,
            usb_combo_changed=True,
            reason="all ring gates passed",
        )

    monkeypatch.setattr(cr, "reconcile_auto", fake_auto)
    rc = cr.main(["--auto"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "coupling auto:" in out and "coupling=shm_ring" in out


def test_cli_auto_and_explicit_are_mutually_exclusive(monkeypatch):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        cr.main(["loopback", "--auto"])


def test_cli_requires_a_choice_or_auto(monkeypatch):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        cr.main([])


# ------------------------------------------------------------ entry flock
# #1233 follow-up: install.sh, the boot oneshot, and the operator CLI can invoke
# the same transition directly. One advisory flock serializes every entry pass
# so their ordered daemon transitions cannot interleave.


def test_entry_lock_acquires_and_stamps_pid(tmp_path):
    import os

    from jasper.fanin import coupling_reconcile as cr

    lock = cr._acquire_entry_lock(tmp_path / "l.lock", timeout_seconds=0.5)
    assert lock.outcome == "acquired" and lock.fh is not None
    # The holder's pid is stamped for the contention log line.
    assert (tmp_path / "l.lock").read_text().strip() == str(os.getpid())
    lock.fh.close()


def test_entry_lock_bounded_wait_then_contended(tmp_path):
    """A second acquisition while held must give up within the bounded wait and
    name the holder — never block open-ended. (flock is per-open-description,
    so a second open() in the SAME process contends like a second process.)"""
    import os
    import time as _time

    from jasper.fanin import coupling_reconcile as cr

    held = cr._acquire_entry_lock(tmp_path / "l.lock", timeout_seconds=0.5)
    assert held.outcome == "acquired"
    t0 = _time.monotonic()
    second = cr._acquire_entry_lock(
        tmp_path / "l.lock", timeout_seconds=0.3, poll_seconds=0.05
    )
    elapsed = _time.monotonic() - t0
    assert second.outcome == "contended" and second.fh is None
    assert str(os.getpid()) in second.detail
    assert elapsed < 5.0  # bounded, not open-ended
    held.fh.close()


def test_entry_lock_fails_open_when_unopenable(tmp_path, caplog):
    """A broken lock path (no /run on a dev host, non-root probe) must not
    brick reconciles: fail-open at WARNING, no lock held."""
    import logging

    from jasper.fanin import coupling_reconcile as cr

    with caplog.at_level(logging.WARNING, logger="jasper.fanin.coupling_reconcile"):
        lock = cr._acquire_entry_lock(
            tmp_path / "missing-dir" / "l.lock", timeout_seconds=0.1
        )
    assert lock.outcome == "unavailable" and lock.fh is None
    assert "entry_lock_unavailable" in caplog.text


def test_cli_proceeds_unserialized_when_lock_unavailable(monkeypatch, tmp_path):
    """The fail-open SAFETY PROPERTY at the level it actually lives: an
    unopenable lock file must NOT brick the reconcile — main() runs the verb
    unserialized (returning its real result), never fails closed. Pins
    _acquire_entry_lock's 'must not brick reconciles' claim end-to-end through
    main(), not just at the helper."""
    from jasper.fanin import coupling_reconcile as cr

    # A lock path whose parent dir does not exist -> os.open raises -> unavailable.
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(tmp_path / "no-such-dir" / "l.lock"))
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    ran = {"auto": 0}

    def fake_auto(*a, **k):
        ran["auto"] += 1
        return cr.AutoResult(
            ok=True,
            owned=True,
            coupling="loopback",
            gadget_present=False,
            usb_combo_changed=False,
            reason="",
        )

    monkeypatch.setattr(cr, "reconcile_auto", fake_auto)

    rc = cr.main(["--auto"])

    assert rc == 0  # verb ran and succeeded despite no lock
    assert ran["auto"] == 1


@pytest.mark.parametrize("argv", [["--auto"], ["loopback"]])
def test_cli_verbs_run_under_entry_lock(monkeypatch, tmp_path, argv):
    """Apply verbs hold the coupling flock for the pass, then release it."""
    import fcntl

    from jasper.fanin import coupling_reconcile as cr

    lock_path = tmp_path / "entry.lock"
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(lock_path))
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)

    observed: dict[str, bool] = {}

    def probe_lock() -> None:
        with open(lock_path, "r+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                observed["held"] = True
            else:
                observed["held"] = False

    def fake_auto(*a, **k):
        probe_lock()
        return cr.AutoResult(
            ok=True,
            owned=True,
            coupling="loopback",
            gadget_present=False,
            usb_combo_changed=False,
            reason="",
        )

    def fake_reconcile(*a, **k):
        probe_lock()
        return cr.CouplingResult(
            ok=True,
            desired="loopback",
            changed=False,
            direction="confirm",
        )

    monkeypatch.setattr(cr, "reconcile_auto", fake_auto)
    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)

    rc = cr.main(argv)
    assert rc == 0
    assert observed == {"held": True}
    # ...and released after the pass: a fresh acquire succeeds immediately.
    again = cr._acquire_entry_lock(lock_path, timeout_seconds=0.2)
    assert again.outcome == "acquired"
    again.fh.close()


def test_cli_auto_aborts_loudly_on_entry_lock_contention(
    monkeypatch, tmp_path, capsys, caplog
):
    """`--auto` (a requested CHANGE) that loses the lock race aborts BEFORE any
    env write or daemon op, exits non-zero (the oneshot lands `failed` ->
    doctor-visible), and says why on stderr + an ERROR event."""
    import logging

    from jasper.fanin import coupling_reconcile as cr

    lock_path = tmp_path / "entry.lock"
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(cr, "ENTRY_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(cr, "ENTRY_LOCK_POLL_SECONDS", 0.05)
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(
        cr,
        "reconcile_auto",
        lambda *a, **k: pytest.fail("verb ran despite lock contention"),
    )

    held = cr._acquire_entry_lock(lock_path, timeout_seconds=0.5)
    assert held.outcome == "acquired"
    try:
        with caplog.at_level(logging.ERROR, logger="jasper.fanin.coupling_reconcile"):
            rc = cr.main(["--auto"])
    finally:
        held.fh.close()

    assert rc == 1
    err = capsys.readouterr().err
    assert str(lock_path) in err and "another reconcile pass" in err
    assert "entry_lock_contended" in caplog.text


# --- shm_ring coupling (Ring A + Ring B, P2) ---------------------------------


@pytest.fixture
def _ring_assets_present(monkeypatch):
    """Fixture wrapper over :func:`force_ring_gates_pass`.

    The body is a plain function so a sibling suite can build its OWN fixture on
    it: importing a fixture into another module registers it there but shadows
    the import with the test's parameter of the same name, which is a lint error
    and reads as an accident. One implementation, two fixtures.
    """
    force_ring_gates_pass(monkeypatch)


def force_ring_gates_pass(monkeypatch):
    """Force the shm_ring activation gates to pass (assets + all geometry axes).

    Every PREFLIGHT must pass for an arm to proceed: assets present, the conf.d
    ring period matching outputd's resolved period, AND the Ring-A slot count
    matching. Tests about the ARM SPINE (order, camilla-failure rollback, disarm)
    stub all of them so they exercise the daemon path; the geometry-mismatch and
    slot-mismatch behaviours have their own dedicated tests below (which do NOT use
    this fixture). The stale-ring-file guard is also stubbed to a no-op so the
    spine tests don't touch /dev/shm.

    ``RING_CONF_D`` points at the SHIPPED conf.d rather than a synthetic one:
    the four-ends wire gate reads both PCM blocks, and the file this repo
    actually installs is the honest fixture for "a box on the shipped wire".
    Claiming assets are present while leaving the path at a location that does
    not exist would make the spine tests exercise a torn-conf.d refusal instead
    of the spine.

    THE IOPLUG CAPABILITY GATE IS STUBBED HERE TOO, and it did not need to be
    until the ring wire's default went wide. While the shipped wire was the
    plugin's own, ``ring_wire_caps_ready`` short-circuited to ok on every box and
    these spine tests never reached a record. Now an undeclared box needs the
    ``wire_format`` capability, and a dev host has neither the ``.so`` nor a
    provenance record — so every arm here would refuse for a reason that is
    correct and is not what these tests are about. What is stubbed is
    ``ring_ioplug_wire_supported`` (the RECORD compare), not
    ``ring_wire_caps_ready`` itself, so the gate still resolves the box's wire
    and still refuses an illegal declaration inside these tests.
    """
    import jasper.ring_assets as ra
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr(
        ra,
        "ring_asset_presence",
        lambda **kw: ra.RingAssetPresence(True, True, True),
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    monkeypatch.setattr(
        ra,
        "ring_geometry_matches_outputd",
        lambda outputd_period_frames, **kw: ra.RingGeometryMatch(
            ok=True,
            conf_period_frames=outputd_period_frames,
            outputd_period_frames=outputd_period_frames,
        ),
    )
    monkeypatch.setattr(
        ra,
        "ring_slot_geometry_matches_conf",
        lambda fanin_n_slots, **kw: ra.RingSlotGeometryMatch(
            ok=True, fanin_n_slots=fanin_n_slots, conf_n_slots=fanin_n_slots
        ),
    )
    monkeypatch.setattr(
        ra,
        "ring_ioplug_wire_supported",
        lambda wire, **kw: ra.RingIoplugWireSupport(
            ok=True,
            needed=ra.ring_wire_capabilities(wire),
            detail="stubbed: the installed ioplug vouches for this wire",
        ),
    )
    # The stale-file guard reads /dev/shm; stub it to a no-op for spine tests.
    monkeypatch.setattr(
        cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None
    )


def _stub_ring_ioplug_wire_supported(monkeypatch) -> None:
    """The one stub `force_ring_gates_pass` carries, for a test that builds its
    OWN inline `ring_asset_presence` / `RING_CONF_D` stubs instead of using the
    `_ring_assets_present` fixture.

    Since the ring wire's resolver default went wide, every UNDECLARED box now
    needs the ioplug's `wire_format` capability (`ring_wire_capabilities`), and a
    dev host carries no provenance record — so the real `ring_wire_caps_ready`
    refuses before these tests ever reach the axis (geometry, slots, ...) they
    are actually isolating. Vouching for whatever wire is asked keeps that gate
    out of their way, same as it is for the spine tests above.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra,
        "ring_ioplug_wire_supported",
        lambda wire, **kw: ra.RingIoplugWireSupport(
            ok=True,
            needed=ra.ring_wire_capabilities(wire),
            detail="stubbed: the installed ioplug vouches for this wire",
        ),
    )


def _pin_narrow_ring_wire() -> None:
    """Declare this box's ring wire NARROW via the isolated FANIN_ENV_PATH.

    :data:`~jasper.fanin_coupling.RING_WIRE_FORMAT_ENV_VAR` is the operator's
    rollback lever — nothing in the repo writes it in production (see its module
    docstring in ``jasper/fanin_coupling.py``); the only way a box carries it is
    a human decision. Writing it here reproduces exactly that decision for a test
    whose SUBJECT is the resolved wire itself, called with no fanin.env of its
    own to declare it in. Mirrors ``_declared_wire`` in
    ``tests/test_ring_ioplug_provenance.py``.
    """
    import jasper.fanin.coupling_reconcile as cr
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    Path(cr.FANIN_ENV_PATH).write_text(
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n", encoding="utf-8"
    )


def test_arm_shm_ring_writes_coherent_pair_in_order(tmp_path, _ring_assets_present):
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert result.ok
    assert result.direction == "arm"
    # Ordered spine: outputd (Ring B reader) -> fanin (Ring A writer) -> camilla.
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    outputd_text = outputd_env.read_text()
    assert read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) == "shm_ring"
    assert read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR) == "2"


def test_arm_shm_ring_refused_when_assets_missing_recovers_to_loopback(
    tmp_path, monkeypatch
):
    import jasper.ring_assets as ra

    # ioplug .so missing -> the activation gate refuses the arm.
    monkeypatch.setattr(
        ra,
        "ring_asset_presence",
        lambda **kw: ra.RingAssetPresence(
            so_present=False, conf_present=True, shm_dir_present=True
        ),
    )
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert not result.ok
    assert result.recovered
    assert "assets" in result.detail.lower()
    # Fail-safe: persisted coupling is back to loopback, not shm_ring.
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    # No fanin restart happened before the assets gate (only the recovery ops).
    assert "camilla:loopback" in calls


def test_arm_shm_ring_camilla_failure_recovers_to_loopback(
    tmp_path, _ring_assets_present
):
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    # camilla reconcile fails for shm_ring specifically -> full rollback.
    calls, ro, rf, rc = _recorder(camilla_fail_for=COUPLING_SHM_RING)
    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert not result.ok
    assert result.recovered
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    # The recovery re-reconciled camilla to loopback.
    assert calls.count("camilla:loopback") >= 1


def test_arm_shm_ring_outputd_restart_failure_recovers_to_loopback(
    tmp_path, _ring_assets_present
):
    # NIT 8: the ring-arm outputd-restart-failure rollback branch (the transport_pipe
    # twin is tested; the ring twin was not). outputd fails to restart -> the arm
    # never reaches fan-in/camilla; the env is rolled BACK to loopback + the ring
    # keys cleared, so a later manual restart lands clean. (recovered is False here
    # because the recovery's OWN outputd restart also fails — the daemon is down —
    # but the persisted env is safely loopback, which is the load-bearing invariant.)
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder(outputd_ok=False)

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.restarted_outputd is False  # outputd never came up
    assert "camilla:shm_ring" not in calls  # never reached the camilla arm
    assert "camilla:loopback" in calls  # recovery reconciled camilla back
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None


def test_arm_shm_ring_fanin_restart_failure_recovers_to_loopback(
    tmp_path, _ring_assets_present
):
    # NIT 8: the ring-arm fanin-restart-failure rollback branch — outputd came up,
    # fan-in failed. The env is rolled back to loopback + ring keys cleared.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder(fanin_ok=False)

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.restarted_outputd is True  # outputd (Ring B reader) came up first
    assert "camilla:shm_ring" not in calls  # never reached the camilla arm
    assert "camilla:loopback" in calls  # recovery reconciled camilla back
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None


def test_arm_shm_ring_refused_on_geometry_mismatch_recovers(tmp_path, monkeypatch):
    # SF3: assets present, but the conf.d ring period (128) != outputd's resolved
    # period (1024 default). Arming would fail CamillaDSP's ring open() with a
    # confusing rollback; the preflight refuses UP FRONT with a crisp reason and
    # recovers to loopback — BEFORE bouncing any daemon.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path)))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "")
    # outputd.env carries no period -> resolves to the packaged default 1024.
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert result.desired == COUPLING_SHM_RING
    # Crisp reason names both periods; not a bare "arm failed".
    assert "128" in result.detail and "1024" in result.detail
    # The ring was NEVER armed — camilla was only reconciled to loopback (the
    # recovery), never to shm_ring. (The recovery itself does restart fanin/outputd.)
    assert "camilla:shm_ring" not in calls
    assert "camilla:loopback" in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_arm_shm_ring_succeeds_when_geometry_matches(tmp_path, monkeypatch):
    # The mirror: when the conf.d period equals outputd's resolved period (128 on
    # the Apple-dongle floor), the geometry gate passes and the arm proceeds.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path)))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True
    assert result.direction == "arm"
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


# --- D5 (wide-output-path program): ring wire-width preflight ----------------


def _break_ring_kwargs_override(monkeypatch, *, playback_format: str | None):
    """Simulate the shm_ring coupling losing its narrow-lane override.

    ``playback_format=None`` drops the key entirely (someone deleted the
    override, so the emitter falls back to the box-wide default); a string sets
    it to a width the ring cannot carry. Patches the module attribute
    ``content_lane_format_for_coupling`` actually calls."""
    import jasper.fanin_coupling as coupling

    real = coupling.capture_kwargs_for_coupling

    def broken(raw):
        kwargs = dict(real(raw))
        if not kwargs:
            return kwargs
        if playback_format is None:
            kwargs.pop("playback_format", None)
        else:
            kwargs["playback_format"] = playback_format
        return kwargs

    monkeypatch.setattr(coupling, "capture_kwargs_for_coupling", broken)


def test_ring_edge_width_ready_passes_on_an_operator_narrow_pinned_box_because_the_coupling_narrows():
    """THE RULING (wide-output-path PR-6), re-pointed at its post-flip subject.

    A ring-coupled box can keep its ring at coherent S16 even though the box-wide
    program lane is S32, because the shm_ring coupling's kwargs FORCE the emitted
    lane to whatever :func:`resolve_ring_wire` resolves. That resolver's DEFAULT
    went wide too (PR #2601), so an UNDECLARED box no longer demonstrates the
    ruling — its ring resolves S32_LE right along with the box-wide lane, and
    nothing narrows. The one shape left where the two constants are genuinely
    different is an operator's narrow pin
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`` — the rollback lever; nothing in
    the repo writes it). Pinning it here is what still exercises the ruling: the
    gate must PASS with ``DEFAULT_PLAYBACK_FORMAT`` and the pinned wire genuinely
    different, the state the pre-PR-6 constant comparison would have refused on
    every ring-eligible box, including jts.local and its certified USB-route
    latency artifact."""
    import jasper.camilla_config_contract as contract
    from jasper.fanin_coupling import RING_WIRE_FORMAT

    _pin_narrow_ring_wire()
    assert contract.DEFAULT_PLAYBACK_FORMAT == "S32_LE"
    assert RING_WIRE_FORMAT == "S16_LE"
    assert contract.DEFAULT_PLAYBACK_FORMAT != RING_WIRE_FORMAT
    ok, detail = ring_edge_width_ready()
    assert ok is True
    assert "S16_LE" in detail


@pytest.mark.parametrize("broken_format", [None, "S32_LE"])
def test_ring_edge_width_ready_refuses_when_the_coupling_stops_narrowing(
    monkeypatch, broken_format
):
    """The invariant the gate now guards: if the coupling ever stops forcing the
    ring's own wire format — the key dropped, or repointed at a wider one —
    arming would mis-transcode every sample, so refuse with a reason naming both
    widths AND the function that must do the forcing.

    Pinned NARROW first (see
    ``test_ring_edge_width_ready_passes_on_an_operator_narrow_pinned_box...``):
    on an undeclared box the coupling's kwargs override and the box-wide default
    it falls back to are the SAME wide token now, so breaking the override would
    leave every declaring end agreeing by accident and this test would prove
    nothing. The pin is what makes "the override stopped forcing narrow" and
    "the box is wide anyway" two different, distinguishable states again.
    """
    _pin_narrow_ring_wire()
    _break_ring_kwargs_override(monkeypatch, playback_format=broken_format)
    ok, detail = ring_edge_width_ready()
    assert ok is False
    assert "S32_LE" in detail
    assert "S16_LE" in detail
    assert "capture_kwargs_for_coupling" in detail
    assert "keeping loopback" in detail


def test_default_ring_gates_order_puts_each_gate_after_what_makes_it_meaningful():
    """Gate ORDER is a diagnostic contract, not an arbitrary tuple.

    Two orderings are load-bearing and both were wrong before R5b generalized
    the wire gate:

    * ``ring_topology`` BEFORE ``ring_edge_width`` — on a box that resolves no
      ring width, ``resolve_ring_wire`` falls back to the shipped stereo
      declaration, so a wire comparison there reports a disagreement that is an
      artefact of the fallback and names the wrong defect. jts3 (roleful, 6-ch
      active lane) is the live case: it must be refused for its topology, not
      for a channel count it never claimed.
    * ``ring_assets`` BEFORE the two gates that READ those assets — the wire
      gate parses the conf.d and the capability gate hashes the ioplug, so a
      missing asset must produce the asset gate's one clear reason rather than a
      parse failure downstream.
    """
    names = [name for name, _ in default_ring_gates()]
    assert names.index("ring_topology") < names.index("ring_edge_width")
    assert names.index("ring_assets") < names.index("ring_edge_width")
    assert names.index("ring_assets") < names.index("ring_wire_caps")
    assert dict(default_ring_gates())["ring_edge_width"] is ring_edge_width_ready


def test_both_arm_paths_read_their_order_from_one_enumeration():
    """#2285: the unattended pass and the manual arm cannot disagree on order.

    They used to. ``default_ring_gates()`` ran topology BEFORE assets while
    ``_arm_ring`` spelled the same four gates out again and ran assets BEFORE
    topology, so a box broken in BOTH ways was told a different first thing to
    fix depending on which path found it — and ``default_ring_gates``'s own
    docstring claimed "in manual-arm order" while describing the order the
    manual arm did not use. Nothing compared them, which is why it drifted.

    The comparison is the point of this test: both paths now derive from
    ``_SHARED_RING_PREFLIGHTS``, and the assertions below fail if either grows a
    second list. Asserting the shared tuple alone would not — a re-introduced
    hand-written sequence inside ``_arm_ring`` would sail past it.
    """
    import inspect

    from jasper.fanin.coupling_reconcile import (
        _arm_ring,
        _SHARED_RING_PREFLIGHTS,
    )

    shared = [name for name, _ in _SHARED_RING_PREFLIGHTS]

    # The unattended pass is the shared spine plus its own prefix, in order.
    unattended = [name for name, _ in default_ring_gates()]
    assert [n for n in unattended if n in shared] == shared

    # The manual arm iterates the shared tuple rather than re-listing the gates.
    # A hand-written `x_ok, x_detail = ring_<gate>_ready()` for a SHARED gate is
    # exactly the drift this closes; the two non-shared geometry gates it calls
    # directly are deliberately not in the tuple.
    arm_src = inspect.getsource(_arm_ring)
    assert "_SHARED_RING_PREFLIGHTS" in arm_src
    for direct_call in (
        "ring_topology_ready_strict()",
        "ring_assets_ready()",
        "ring_wire_caps_ready()",
    ):
        assert direct_call not in arm_src, (
            f"{direct_call} is called directly in _arm_ring — that is a second "
            "enumeration of a shared gate, which is how the orders drifted"
        )


def test_arm_shm_ring_refused_on_broken_narrowing_recovers_to_loopback(
    tmp_path, monkeypatch, _ring_assets_present
):
    """The manual-arm chain wiring: a coupling that has lost its narrow-lane
    override refuses the arm BEFORE any daemon is bounced and recovers to
    loopback — the belt to the coupling_auto default-resolution suspender
    covered above.

    OPERATOR NARROW PIN, not an undeclared box: since the ring wire resolver's
    default went wide, an undeclared box's kwargs override and the wide default
    it would fall back to are the SAME token, so "the override stopped forcing
    narrow" is invisible there — see
    ``test_ring_edge_width_ready_refuses_when_the_coupling_stops_narrowing``.
    Pinning the box narrow (via the isolated JASPER_ENV_PATH, which both
    :func:`resolve_ring_wire` and the "fan-in" declaring end's fallback read)
    plus a conf.d that ALSO spells the narrow token — matching what a real
    narrow-pinned box's re-rendered conf.d would say, unlike the shipped file
    ``_ring_assets_present`` points at, which spells S32_LE unconditionally —
    reproduces the box this test is actually about.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    jasper_env = _write(
        tmp_path / "jasper.env", f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n"
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "RING_CONF_D", str(_ring_conf(tmp_path, sample_format=RING_WIRE_FORMAT))
    )
    _break_ring_kwargs_override(monkeypatch, playback_format=None)
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert "S32_LE" in result.detail and "S16_LE" in result.detail
    # The ring was NEVER armed — camilla was only reconciled to loopback (the
    # recovery), never to shm_ring. (The recovery itself does restart
    # fanin/outputd, same as every other preflight-refusal test above.)
    assert "camilla:shm_ring" not in calls
    assert "camilla:loopback" in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


# --- defect A: Ring-A slot-count coherence + stale-file guard + migration -----


def _ring_conf(
    tmp_path,
    *,
    capture_n_slots: int = 2,
    period_frames: int = 128,
    sample_format: str = "S32_LE",
):
    """Write a ring conf.d with a configurable jts_ring_capture n_slots.

    period_frames stays 128 (the Apple-dongle floor) so the SEPARATE period gate
    passes when outputd's env carries JASPER_OUTPUTD_PERIOD_FRAMES=128; these tests
    isolate the slot axis.

    ``sample_format`` defaults to ``S32_LE`` — the token the SHIPPED conf.d now
    spells explicitly in every block (``deploy/alsa/conf.d/60-jts-ring.conf``).
    Without a ``format`` line here, an UNDECLARED box's ground-truth wire
    (``resolve_ring_wire_format``, wide by default) would disagree with this
    hand-rolled conf.d's implicit ioplug-default declaration — an omitted
    ``format`` key still declares a wire, just the narrow one
    (``jasper.ring_assets.ring_conf_format``'s absent-means-default contract) —
    tripping ``ring_edge_width_ready`` for a reason unrelated to whatever axis
    (slots/period) the calling test actually isolates. Pass ``"S16_LE"`` for a
    test that means to reproduce an operator's narrow-pinned box instead.
    """
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames {period_frames}\n"
        f"    n_slots {capture_n_slots}\n    format {sample_format}\n}}\n"
        f"pcm.jts_ring_playback {{\n    period_frames {period_frames}\n"
        f"    n_slots 2\n    format {sample_format}\n}}\n",
        encoding="utf-8",
    )
    return conf


def test_arm_converges_the_content_format_before_any_restart(
    tmp_path, _ring_assets_present
):
    """jts4's first-arm reboot, pinned as an ORDER.

    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written only by
    jasper-audio-hardware-reconcile, so on a first arm nothing had re-derived it
    from the new coupling by the time the spine restarted outputd: outputd came
    up still asking for the loopback lane's S32_LE while CamillaDSP's ioplug
    attached the ring at S16_LE, which is an attach_fatal — jasper-camilla
    crash-looped into StartLimitAction=reboot and rebooted the speaker
    (2026-08-14). The converge must therefore precede EVERY restart, not merely
    happen somewhere in the arm.

    Asserted as a full ordered list rather than an index comparison: the spine's
    own order — outputd, then fan-in, then CamillaDSP — is itself validated (the
    reader is up before the writer, and Camilla attaches the ring last), so a
    converge that displaced one of those would be a different bug.
    """
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    def kick():
        calls.append("converge-content-format")
        return (True, "")

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=kick,
    )

    assert result.ok is True, result.detail
    assert calls == [
        "converge-content-format",
        "outputd",
        "fanin",
        "camilla:shm_ring",
    ]
    # The coupling the converge reads is already persisted when it runs — that is
    # what lets the single writer derive the RING wire rather than the old one.
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_arm_refuses_when_the_content_format_converge_fails(
    tmp_path, _ring_assets_present
):
    """Fail-CLOSED: no converge, no arm — and specifically, no outputd restart.

    Restarting outputd after a failed converge is exactly the reboot path above,
    so the refusal must land BEFORE the spine. The box recovers to loopback with
    a reason naming the key and the remedy.
    """
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=lambda: (False, "unit failed"),
    )

    assert result.ok is False
    assert result.recovered is True
    assert "JASPER_OUTPUTD_CONTENT_FORMAT" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail
    # Recovery bounces the box back to loopback; what must NOT appear is an
    # outputd restart while the coupling was still shm_ring.
    assert calls[:1] != ["outputd"]
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_a_refused_converge_does_not_re_kick_but_a_timed_out_one_does(
    tmp_path, _ring_assets_present
):
    """The timeout/refusal asymmetry, as TWO runs of one scenario.

    Both outcomes refuse the arm identically, so `ok`/`recovered`/the persisted
    coupling cannot tell them apart — which is exactly why adding a re-converge
    to the REFUSAL branch was undetectable before this test existed. The only
    observable is how many times the kick op is called, so that is what is
    asserted, in both directions:

      - REFUSED (the unit ran and exited non-zero): nothing is in flight, the
        rollback's env write is the last one, so a second kick is pointless work
        that would fail the same way. ONE call.
      - TIMED OUT: the oneshot may still be RUNNING, holding the pre-rollback
        `shm_ring` it read on entry, and can write the ring wire AFTER recovery
        writes loopback back. A second kick settles the order (systemd
        serialises starts of one unit). TWO calls.
    """
    def run(kick_detail: str) -> list[str]:
        fanin_env = _write(tmp_path / f"fanin-{len(kick_detail)}.env", "")
        outputd_env = _write(tmp_path / f"outputd-{len(kick_detail)}.env", "")
        calls, ro, rf, rc = _recorder()

        def kick():
            calls.append("converge")
            return (False, kick_detail)

        result = _reconcile(
            COUPLING_SHM_RING,
            fanin_env=fanin_env,
            outputd_env=outputd_env,
            restart_outputd=ro,
            restart_fanin=rf,
            reconcile_camilla=rc,
            active_leader_check=lambda: False,
            kick_hardware_reconcile=kick,
        )
        # Both outcomes are refusals — this is the part that does NOT distinguish
        # them, asserted so the test cannot pass by the two runs diverging here.
        assert result.ok is False
        assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
        return calls

    refused = run("Job for jasper-audio-hardware-reconcile.service failed")
    assert refused.count("converge") == 1, (
        "a refused converge has nothing in flight; a second kick is pointless "
        f"work that fails the same way (calls={refused})"
    )

    timed_out = run(
        "Command '['systemctl', 'start', "
        "'jasper-audio-hardware-reconcile.service']' timed out after 60.0 seconds"
    )
    assert timed_out.count("converge") == 2, (
        "a timed-out converge may still be running and can write the ring wire "
        f"behind the rollback; it must be re-run after it (calls={timed_out})"
    )


def test_kick_timed_out_matches_both_real_broker_timeout_strings():
    """The classifier's fragile half, pinned against the REAL exception strings.

    `kick_timed_out` matches on the broker's wording, so a broker-side re-word
    would silently downgrade every timeout to a refusal — the fail-OPEN
    direction, and invisible, because a refusal is also a refused arm. These are
    constructed from the actual exception types the two broker paths raise
    rather than from copied literals, so the pin tracks the real vocabulary.

    Negative cases matter as much: an ordinary unit failure must NOT read as a
    timeout, or every refusal would pay a pointless second kick.
    """
    import socket
    import subprocess

    from jasper.fanin import coupling_reconcile as cr

    # Path 1: the exec bound, on the broker thread and the root direct fallback
    # alike (`subprocess.run(..., timeout=exec_timeout)`).
    exec_timeout = subprocess.TimeoutExpired(
        ["systemctl", "start", "jasper-audio-hardware-reconcile.service"], 60.0
    )
    assert cr.kick_timed_out(str(exec_timeout))
    # Path 2: the client socket deadline, surfaced as BrokerUnavailable.
    assert cr.kick_timed_out(f"restart broker unavailable: {socket.timeout('timed out')}")

    # Negatives: real refusal shapes the broker actually produces.
    assert not cr.kick_timed_out("rc=1")
    assert not cr.kick_timed_out("unit(s) not in allowlist: something.service")
    assert not cr.kick_timed_out(
        "Job for jasper-audio-hardware-reconcile.service failed"
    )
    assert not cr.kick_timed_out("")


def test_arm_reconverges_the_content_format_when_a_later_stage_fails(
    tmp_path, _ring_assets_present
):
    """A recovered box must not keep asking for the ring's width.

    The converge lands before the spine, so a spine failure recovers to loopback
    with JASPER_OUTPUTD_CONTENT_FORMAT still at the ring wire — narrower than the
    loopback lane's program default. The plug-wrapped passive lane would silently
    requantize it and the RAW active lane would refuse the open, so the failure
    handler kicks the same single writer again, this time against the loopback
    coupling recovery has just written back.
    """
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder(camilla_ok=False)

    def kick():
        calls.append("converge-content-format")
        return (True, "")

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=kick,
    )

    assert result.ok is False
    # Two converges: one before the spine, one after recovery wrote loopback.
    assert calls.count("converge-content-format") == 2
    assert calls[0] == "converge-content-format"
    assert calls[-1] == "converge-content-format"
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_geometry_gate_honours_an_outputd_period_from_the_jasper_env_position(
    tmp_path, monkeypatch
):
    """The gate must model outputd's TWO-file env chain, not just outputd.env.

    jasper-outputd.service loads /etc/jasper/jasper.env first and outputd.env
    last. The documented operator seam puts JASPER_OUTPUTD_PERIOD_FRAMES in the
    FIRST file, and ``outputd_latency_floor_actions`` honours it by REMOVING the
    generated key from the second — so on a seam-configured box the period lives
    ONLY in jasper.env. Reading outputd.env alone reported the 1024 packaged
    default and refused the arm while the running outputd was correctly at 128
    (jts4, 2026-08-14).

    The conf.d here pins 128, so the gate passes iff it reads the seam.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    jasper_env = _write(
        tmp_path / "jasper.env",
        "JASPER_OUTPUTD_PERIOD_FRAMES=128\nJASPER_OUTPUTD_DAC_BUFFER_FRAMES=512\n",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )
    # The seam's whole shape: the generated key is ABSENT from outputd.env.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_geometry_gate_prefers_outputd_env_over_the_jasper_env_position(
    tmp_path, monkeypatch
):
    """The chain's DIRECTION, so the fix cannot be satisfied by reading either file.

    outputd.env is loaded LAST and therefore wins. A jasper.env value that agrees
    with the conf.d must NOT rescue an outputd.env value that disagrees — that
    would be a gate passing a geometry the daemon will not run.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    jasper_env = _write(tmp_path / "jasper.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(
        tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=1024\n"
    )
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert "1024" in result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_arm_shm_ring_refused_on_slot_mismatch_recovers(tmp_path, monkeypatch):
    # Defect A: assets + period match, but fan-in's JASPER_FANIN_RING_SLOTS resolves
    # to a value != the conf.d jts_ring_capture n_slots. This is the 2026-07-05 hole
    # the period gate did NOT cover. The preflight refuses UP FRONT with a crisp
    # reason and recovers to loopback — BEFORE bouncing any daemon.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    # conf.d pins n_slots 4; fan-in env resolves to the product default 2, a
    # genuine custom-conf mismatch that the stale-env migration cannot repair.
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=4)))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert result.desired == COUPLING_SHM_RING
    # Crisp reason names both slot counts; not a bare "arm failed".
    assert "n_slots=2" in result.detail and "n_slots=4" in result.detail
    # The ring was NEVER armed — camilla only reconciled to loopback (recovery).
    assert "camilla:shm_ring" not in calls
    assert "camilla:loopback" in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_arm_shm_ring_migrates_stale_ring_slots_then_arms(tmp_path, monkeypatch):
    # Default migration: a stale JASPER_FANIN_RING_SLOTS=8 old-default line that
    # disagrees with the conf.d's pinned 2 is overridden in fanin.env at arm time
    # (self-heals to the coherent default) so the arm proceeds instead of being
    # blocked forever.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    # No on-disk stale ring in this test (macOS has no /dev/shm; the guard no-ops on
    # an absent file). The migration is the axis under test.
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "JASPER_FANIN_RING_SLOTS=8\n")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    # The stale =8 line was overridden in fanin.env (the later systemd env file).
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


def test_arm_shm_ring_overrides_stale_base_ring_slots_then_arms(tmp_path, monkeypatch):
    # Regression for the real systemd env chain: jasper-fanin.service loads
    # /etc/jasper/jasper.env first and fanin.env last. A stale base-env =8 is
    # still live when fanin.env has no slot override, so migration must write an
    # explicit coherent =2 into fanin.env rather than merely relying on defaults.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    jasper_env = _write(tmp_path / "jasper.env", "JASPER_FANIN_RING_SLOTS=8\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


def test_arm_shm_ring_keeps_matching_operator_ring_slots(tmp_path, monkeypatch):
    # A JASPER_FANIN_RING_SLOTS that MATCHES the conf.d is a coherent operator
    # override — the migration must NOT strip it (it only strips shear-prone
    # residue). conf.d pins 4, env sets 4 → kept, arm proceeds.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=4)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "JASPER_FANIN_RING_SLOTS=4\n")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    # The matching operator override is preserved.
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "4"


def test_arm_shm_ring_deletes_stale_on_disk_ring_before_arming(tmp_path, monkeypatch):
    # Defect A stale-file guard: an on-disk program.ring with a MISMATCHED
    # geometry (an 8-slot file from before the 2-slot default) is deleted before
    # the daemons bounce, so the writer re-creates it fresh. A geometry-matched
    # file is left untouched.
    import struct

    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
        # The WIRE axes a real fan-in writer publishes. They are compared
        # now (R5b), so a header left zeroed here would read as a genuine
        # format shear rather than as the coherent ring these tests mean.
        struct.pack_into("<I", hdr, 8, 48000)  # rate
        struct.pack_into("<I", hdr, 12, 2)  # channels
        struct.pack_into("<I", hdr, 16, 2)  # sample_format = S32LE — the resolver's
        # default since the ring-wire flip; a hardcoded S16LE here would make
        # EVERY ring file these helpers write read as a format shear against the
        # now-wide resolved wire, regardless of the slots/period axis under test.
        struct.pack_into("<I", hdr, 20, 128)  # period_frames
        struct.pack_into("<I", hdr, 24, n_slots)  # n_slots
        path.write_bytes(bytes(hdr) + b"\x00" * 512)

    # Stale Ring A (8 slots vs conf.d's 2) → must be deleted.
    _write_ring(program, 8)
    # Coherent Ring B (2 slots == conf.d's jts_ring_playback 2) → must be KEPT.
    _write_ring(content, 2)

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert not program.exists(), "stale mismatched Ring A must be deleted before arm"
    assert content.exists(), "coherent Ring B must be left untouched"


def test_arm_shm_ring_refused_on_invalid_ring_slots_value(tmp_path, monkeypatch):
    # An out-of-range JASPER_FANIN_RING_SLOTS (a shear-prone value) fails LOUD — the
    # migration does not strip it (it isn't a clean integer that could self-heal to
    # a coherent default), and the preflight refuses with a crisp reason.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    fanin_env = _write(tmp_path / "fanin.env", "JASPER_FANIN_RING_SLOTS=99\n")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert "out of range" in result.detail
    assert "camilla:shm_ring" not in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def _coherent_shm_ring_outputd_text(*, period_frames: int = 128) -> str:
    """outputd.env text already at the coherent shm_ring set (Ring B bridge).

    Matches exactly what ``_outputd_actions(shm_ring)`` writes, so a reconcile with
    the fanin.env already at shm_ring sees ``changed=False`` and takes the CONFIRM
    path — the branch the defect-A CONFIRM-path fix exercises.

    ALSO carries ``JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE`` — the key
    ``jasper-audio-hardware-reconcile`` (the key's single writer) would have
    already re-derived on a genuinely armed box. Without it, ``ring_edge_width_ready``
    reads outputd's declaration as its own stale default (``S16_LE`` — see
    ``_OUTPUTD_DEFAULT_CONTENT_FORMAT`` in coupling_reconcile.py, which does NOT
    follow the ring wire resolver by design), which now disagrees with the
    resolved wire's wide default and refuses every CONFIRM-path test here for a
    reason unrelated to whatever axis (slots/period) it is isolating.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_RING_PATH,
        DEFAULT_OUTPUTD_RING_SLOTS,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        OUTPUTD_RING_PATH_ENV_VAR,
        OUTPUTD_RING_SLOTS_ENV_VAR,
    )

    return (
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={OUTPUTD_CONTENT_BRIDGE_SHM_RING}\n"
        f"{OUTPUTD_RING_PATH_ENV_VAR}={DEFAULT_OUTPUTD_RING_PATH}\n"
        f"{OUTPUTD_RING_SLOTS_ENV_VAR}={DEFAULT_OUTPUTD_RING_SLOTS}\n"
        f"JASPER_OUTPUTD_PERIOD_FRAMES={period_frames}\n"
        "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"
    )


def test_confirm_shm_ring_migrates_old_eight_slot_ring_file_to_default_two_slot(
    tmp_path, monkeypatch
):
    # Default-flip migration: after deploy, conf.d pins the new 2-slot default but
    # tmpfs may still contain an 8-slot program.ring from the old armed runtime.
    # A no-op CONFIRM reconcile must notice the stale header, run the full arm
    # self-heal, delete the stale ring before any Camilla reload, and emit the
    # chunk-128 / target-128 / queue-1 ring config.
    import struct

    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots, period_frames=128):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
        # The WIRE axes a real fan-in writer publishes. They are compared
        # now (R5b), so a header left zeroed here would read as a genuine
        # format shear rather than as the coherent ring these tests mean.
        struct.pack_into("<I", hdr, 8, 48000)  # rate
        struct.pack_into("<I", hdr, 12, 2)  # channels
        struct.pack_into("<I", hdr, 16, 2)  # sample_format = S32LE — the resolver's
        # default since the ring-wire flip; a hardcoded S16LE here would make
        # EVERY ring file these helpers write read as a format shear against the
        # now-wide resolved wire, regardless of the slots/period axis under test.
        struct.pack_into("<I", hdr, 20, period_frames)
        struct.pack_into("<I", hdr, 24, n_slots)
        path.write_bytes(bytes(hdr) + b"\x00" * 512)

    _write_ring(program, 8)  # old Ring A default from the already-armed box
    _write_ring(content, 2)  # Ring B was already minimal

    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
    )
    outputd_env = _write(tmp_path / "outputd.env", _coherent_shm_ring_outputd_text())
    calls: list[str] = []

    def restart_outputd() -> tuple[bool, str]:
        calls.append("outputd")
        return True, ""

    def restart_fanin() -> tuple[bool, str]:
        calls.append("fanin")
        return True, ""

    def reconcile_camilla(coupling: str) -> tuple[bool, str]:
        assert coupling == COUPLING_SHM_RING
        assert not program.exists(), (
            "stale Ring A must be deleted before Camilla reload"
        )
        assert content.exists(), "coherent Ring B must be preserved"
        from jasper.sound.camilla_yaml import emit_flat_ring_config

        yaml = emit_flat_ring_config()
        assert 'device: "jts_ring_capture"' in yaml
        assert 'device: "jts_ring_playback"' in yaml
        assert "chunksize: 128" in yaml
        assert "target_level: 128" in yaml
        assert "queuelimit: 1" in yaml
        assert "enable_rate_adjust: false" in yaml
        calls.append(f"camilla:{coupling}")
        return True, "reconciled"

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=restart_outputd,
        restart_fanin=restart_fanin,
        reconcile_camilla=reconcile_camilla,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert not program.exists()
    assert content.exists()
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") is None


def test_confirm_shm_ring_self_heals_stale_ring_slots(tmp_path, monkeypatch):
    # CONFIRM-path migration: a box ALREADY armed shm_ring with a stale
    # JASPER_FANIN_RING_SLOTS=8 line — CamillaDSP crash-looping on the ioplug
    # geometry mismatch — was NOT healed by a reconcile, because the coupling-flip
    # write didn't change (already shm_ring) so the arm self-heal never ran and the
    # CONFIRM path only re-loaded camilla. The fix: the CONFIRM path detects the
    # incoherence and escalates to the full _arm_ring spine (overrides the stale line
    # THEN bounces the daemons). This is the literal state the doctor's remediation
    # string points at, so it must now actually heal.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)

    # ALREADY armed shm_ring + the stale =8 residue → CONFIRM path (changed=False).
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\nJASPER_FANIN_RING_SLOTS=8\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", _coherent_shm_ring_outputd_text())
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    # The CONFIRM path escalated to the full arm spine (self-heal THEN bounce), NOT a
    # lightweight camilla-only confirm.
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    # The stale =8 line was overridden in fanin.env.
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_confirm_shm_ring_self_heals_stale_base_ring_slots(tmp_path, monkeypatch):
    # Already-armed CONFIRM path, but the stale slot value lives in the earlier
    # /etc/jasper/jasper.env layer. This must still escalate to _arm_ring and
    # write the later fanin.env override before the daemon bounce.
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(tmp_path / "program.ring"))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(tmp_path / "content.ring"))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    jasper_env = _write(tmp_path / "jasper.env", "JASPER_FANIN_RING_SLOTS=8\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", _coherent_shm_ring_outputd_text())
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_confirm_shm_ring_coherent_stays_lightweight(tmp_path, monkeypatch):
    # The other side of the CONFIRM-path fix: a COHERENT already-armed shm_ring box
    # must NOT bounce fan-in/outputd on every reconcile tick — only re-load camilla.
    # This pins that the escalation is gated on POSITIVE incoherence evidence, so a
    # healthy box keeps the cheap confirm (the property the reviewer flagged as the
    # regression risk of over-eagerly always running _arm_ring).
    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2)))
    _stub_ring_ioplug_wire_supported(monkeypatch)
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    import struct

    def _write_ring(path, n_slots):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
        # The WIRE axes a real fan-in writer publishes. They are compared
        # now (R5b), so a header left zeroed here would read as a genuine
        # format shear rather than as the coherent ring these tests mean.
        struct.pack_into("<I", hdr, 8, 48000)  # rate
        struct.pack_into("<I", hdr, 12, 2)  # channels
        struct.pack_into("<I", hdr, 16, 2)  # sample_format = S32LE — the resolver's
        # default since the ring-wire flip; a hardcoded S16LE here would make
        # EVERY ring file these helpers write read as a format shear against the
        # now-wide resolved wire, regardless of the slots/period axis under test.
        struct.pack_into("<I", hdr, 20, 128)
        struct.pack_into("<I", hdr, 24, n_slots)
        path.write_bytes(bytes(hdr) + b"\x00" * 512)

    _write_ring(program, 2)
    _write_ring(content, 2)

    # Armed shm_ring, env slots MATCH the conf.d (coherent operator override kept).
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\nJASPER_FANIN_RING_SLOTS=2\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", _coherent_shm_ring_outputd_text())
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    assert result.direction == "confirm"
    assert not result.changed
    # Lightweight: camilla-only re-load, NO fan-in / outputd bounce.
    assert calls == ["camilla:shm_ring"]
    # The coherent operator override is preserved.
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert program.exists()
    assert content.exists()


def test_confirm_shm_ring_self_heals_stale_on_disk_period(tmp_path, monkeypatch):
    # CONFIRM-path self-heal on the SECOND on-disk geometry axis (period_frames): an
    # on-disk program.ring whose n_slots MATCHES the conf.d but whose period_frames
    # is stale still fails the ioplug attach. The CONFIRM path must escalate to the
    # arm, and the arm's stale-file delete must remove it (defect A + Nit-7
    # period_frames axis).
    import struct

    import jasper.ring_assets as ra

    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    # conf.d pins period_frames=128, n_slots=2.
    monkeypatch.setattr(
        ra,
        "RING_CONF_D",
        str(_ring_conf(tmp_path, capture_n_slots=2, period_frames=128)),
    )
    _stub_ring_ioplug_wire_supported(monkeypatch)
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots, period_frames):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
        # The WIRE axes a real fan-in writer publishes. They are compared
        # now (R5b), so a header left zeroed here would read as a genuine
        # format shear rather than as the coherent ring these tests mean.
        struct.pack_into("<I", hdr, 8, 48000)  # rate
        struct.pack_into("<I", hdr, 12, 2)  # channels
        struct.pack_into("<I", hdr, 16, 2)  # sample_format = S32LE — the resolver's
        # default since the ring-wire flip; a hardcoded S16LE here would make
        # EVERY ring file these helpers write read as a format shear against the
        # now-wide resolved wire, regardless of the slots/period axis under test.
        struct.pack_into("<I", hdr, 20, period_frames)
        struct.pack_into("<I", hdr, 24, n_slots)
        path.write_bytes(bytes(hdr) + b"\x00" * 512)

    # Matching slots (2) but STALE period (256 vs conf.d 128) → must be deleted.
    _write_ring(program, 2, 256)
    # Coherent Ring B (2 slots, 128 period) → kept.
    _write_ring(content, 2, 128)

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\nJASPER_FANIN_RING_SLOTS=2\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", _coherent_shm_ring_outputd_text())
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is True, result.detail
    # Escalated to the full arm spine and the stale-period Ring A was deleted.
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert not program.exists(), (
        "stale-period Ring A must be deleted on CONFIRM self-heal"
    )
    assert content.exists(), "coherent Ring B must be left untouched"


def test_arm_shm_ring_refused_on_ineligible_topology_recovers(
    tmp_path, monkeypatch, _ring_assets_present
):
    # SF4: a non-ring-eligible saved topology (composite/roleful/mono) must be
    # refused UP FRONT via the topology_supports_shm_ring predicate — a crisp reason
    # instead of failing later at outputd's Rust full-range-stereo rejection.
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_contract.topology_supports_shm_ring",
        lambda topology: False,
    )
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert result.desired == COUPLING_SHM_RING
    assert "ring-eligible" in result.detail
    # DEFECT 2: the refusal names the actionable remediation for a plain-stereo
    # box carrying stale roleful/subwoofer artifacts (jts.local's shape).
    assert "jasper-output-topology-reset" in result.detail
    assert "camilla:shm_ring" not in calls  # never armed
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_arm_shm_ring_topology_unreadable_now_fails_closed(
    tmp_path, monkeypatch, _ring_assets_present
):
    """An UNREADABLE topology REFUSES the operator arm. Direction flipped, on purpose.

    This gate used to fail OPEN here, on the stated grounds that "outputd's own
    guard is the backstop". That backstop was then proven to fail open on the
    SAME error: a topology read failure clears the reconciler's active-lane
    marker, outputd's ``is_full_range_stereo_lr_sink`` predicate goes true, and
    it attaches whichever ring it was pointed at as an ordinary stereo sink — on
    a 2-way box the widths are equal, so nothing downstream can tell. The ring-v2
    allowlist restores a real backstop, but the arm stays fail-CLOSED regardless:
    an operator is standing at the keyboard when this runs, so a refusal costs
    them one rerun, where admitting costs a parked speaker.

    Fail-CLOSED means the ordinary recovery: nothing armed, no daemon bounced
    past the preflight, coupling persisted at loopback.
    """
    from jasper.output_topology import OutputTopologyError

    def _boom(*a, **k):
        raise OutputTopologyError("corrupt topology")

    monkeypatch.setattr("jasper.output_topology.load_output_topology_strict", _boom)
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert "topology unreadable" in result.detail
    assert "fail-closed" in result.detail
    assert "camilla:shm_ring" not in calls  # never armed
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


# --- DEFECT 2: ring_topology_ready end-to-end over REAL on-disk topologies ----
# The tests above either exercise the topology_supports_shm_ring predicate in
# isolation (tests/test_runtime_contract_ring.py) or MOCK the predicate at the
# reconciler seam. Neither proves the actual arming gate the defect names
# (arm_ring_topology_ineligible) resolves a REAL topology loaded from disk — nor
# that a real stale-subwoofer topology honestly refuses through that same gate.
# These close that gap: an unconfigured draft must remain silent until a
# passive stereo layout is saved, while the stale-subwoofer refusal stays clear.


def test_fresh_unconfigured_topology_refuses_ring_then_explicit_stereo_allows_it(
    tmp_path,
    monkeypatch,
    _ring_assets_present,
):
    # Empty speaker_groups is deliberately not passive stereo: it is an
    # unconfigured speaker, so the ring must not create audible output until the
    # owner saves an explicit passive stereo layout.
    from jasper.fanin.coupling_reconcile import ring_topology_ready
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )

    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    topology = OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "default",
            "name": "Speaker outputs",
            "status": "draft",
            "hardware": {
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_count": 2,
                "card_id": "A",
                "outputs": [
                    {
                        "index": 0,
                        "human_label": "Left",
                        "terminal_label": "1",
                        "state": "unused",
                    },
                    {
                        "index": 1,
                        "human_label": "Right",
                        "terminal_label": "2",
                        "state": "unused",
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
        }
    )
    save_output_topology(topology)

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    refused = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    ok, detail = ring_topology_ready()

    assert refused.ok is False
    assert refused.recovered is True
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert "camilla:shm_ring" not in calls
    assert ok is False
    assert "no speaker layout is configured" in detail
    assert "passive stereo" in detail

    configured = topology.to_dict()
    configured["speaker_groups"] = [
        {
            "id": "left",
            "label": "Left",
            "kind": "left",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        },
        {
            "id": "right",
            "label": "Right",
            "kind": "right",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 1}],
        },
    ]
    configured["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right",
    }
    save_output_topology(OutputTopology.from_mapping(configured))

    ok, detail = ring_topology_ready()

    calls.clear()
    armed = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert ok is True
    assert "declared passive stereo" in detail
    assert armed.ok is True
    assert armed.direction == "arm"
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_ring_topology_ready_refuses_real_stale_subwoofer_with_reset_hint(
    tmp_path,
    monkeypatch,
):
    # The negative end-to-end path over a REAL topology (not a mocked predicate):
    # a plain Apple-dongle box whose SAVED topology still declares a subwoofer role
    # from the 2026-06 campaign refuses through ring_topology_ready() — a stereo
    # ring genuinely cannot drive a sub — and the refusal names the actionable
    # remediation (jasper-output-topology-reset) instead of an opaque "loopback".
    from jasper.fanin.coupling_reconcile import ring_topology_ready
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )

    topo_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topo_path))
    save_output_topology(
        OutputTopology.from_mapping(
            {
                "artifact_schema_version": 1,
                "kind": OUTPUT_TOPOLOGY_KIND,
                "topology_id": "default",
                "name": "Speaker outputs",
                "status": "draft",
                "hardware": {
                    "device_id": "apple_usb_c_dongle",
                    "device_label": "Apple USB-C audio adapter",
                    "physical_output_count": 2,
                    "card_id": "A",
                },
                "speaker_groups": [
                    {
                        "id": "sub",
                        "label": "Subwoofer",
                        "kind": "subwoofer",
                        "mode": "subwoofer",
                        "channels": [{"role": "subwoofer", "physical_output_index": 0}],
                    }
                ],
                "routing": {"subwoofer_group_ids": ["sub"]},
            }
        )
    )

    ok, detail = ring_topology_ready()

    assert ok is False
    assert "jasper-output-topology-reset" in detail


def test_disarm_shm_ring_clears_ring_bridge_keys(tmp_path, _ring_assets_present):
    # Pre-arm to shm_ring, then disarm to loopback.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_SINK=dual_apple\n")
    calls, ro, rf, rc = _recorder()
    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert (
        read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
        == "shm_ring"
    )

    calls.clear()
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert result.ok
    assert result.direction == "disarm"
    # Disarm order: camilla off ring first, then fanin, then outputd.
    assert calls == ["camilla:loopback", "fanin", "outputd"]
    outputd_text = outputd_env.read_text()
    # Ring B keys cleared; the operator's own line survives.
    assert read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None
    assert "JASPER_OUTPUTD_SHM_RING" not in outputd_text
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_text


def test_disarm_shm_ring_kicks_audio_hardware_reconcile_after_outputd(
    tmp_path, _ring_assets_present
):
    """#1231 follow-up: leaving a LIVE shm_ring bridge kicks
    jasper-audio-hardware-reconcile AFTER the ordered disarm ops. The hardware
    reconciler unsets the route's outputd content-buffer floor while the bridge
    is shm_ring (inert there), so without the kick a disarmed box sits on
    outputd's compile-default content buffer until the next udev/boot/deploy
    event — there is no timer for that reconciler.

    The ARM kicks the same oneshot for a DIFFERENT key and at the OPPOSITE end
    of the spine (the jts4 first-arm reboot: JASPER_OUTPUTD_CONTENT_FORMAT must
    reach the ring wire BEFORE outputd restarts), so this pins WHERE each kick
    lands, not merely that one happened."""
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    def kick():
        calls.append("hw-reconcile")
        return (True, "")

    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=kick,
    )
    # ARM kicks FIRST: the content-format converge precedes the whole spine.
    assert calls == ["hw-reconcile", "outputd", "fanin", "camilla:shm_ring"]

    calls.clear()
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=kick,
    )
    assert result.ok
    assert result.direction == "disarm"
    # Kick runs AFTER the ordered disarm (camilla -> fanin -> outputd), so the
    # hardware reconciler sees the settled direct state and its own conditional
    # outputd restart picks the re-emitted floor up.
    assert calls == ["camilla:loopback", "fanin", "outputd", "hw-reconcile"]


def test_disarm_without_live_shm_ring_bridge_does_not_kick_hardware_reconcile(tmp_path):
    """The floor is only suppressed under a LIVE shm_ring outputd bridge; an
    already-direct disarm (outputd not on shm_ring) never had it suppressed, so no
    kick — the disarm keeps its existing blast radius."""
    # fan-in resolves shm_ring but outputd is direct (no CONTENT_BRIDGE=shm_ring):
    # disarming to loopback moves the coupling, but leaves no live ring bridge.
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        "loopback",
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        kick_hardware_reconcile=lambda: (calls.append("hw-reconcile"), (True, ""))[1],
    )

    assert res.ok and res.direction == "disarm"
    assert calls == ["camilla:loopback", "fanin", "outputd"]


def test_disarm_kick_failure_is_best_effort(tmp_path, _ring_assets_present):
    """A failed floor-reemit kick must not fail the disarm: the interim
    compile-default content buffer is a LARGER cushion than the floor
    (fail-safe) and the next hardware-reconcile event still converges it. The
    failure is carried in ``detail`` (observable, never silent)."""
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
        kick_hardware_reconcile=lambda: (False, "broker unavailable"),
    )

    assert result.ok is True
    assert result.direction == "disarm"
    assert "audio-hardware reconcile kick failed" in result.detail
    assert "broker unavailable" in result.detail


def test_blocked_shm_ring_force_disarm_kicks_audio_hardware_reconcile(tmp_path):
    """The route-block force-disarm (a previously-armed shm_ring box that is now
    grouping-enabled) leaves the same suppressed-floor state as an ordinary
    disarm, so its recovery `_disarm` gets the same gated kick — after the
    ordered camilla -> fanin -> outputd recovery ops."""
    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
        kick_hardware_reconcile=lambda: (calls.append("hw-reconcile"), (True, ""))[1],
    )

    assert res.direction == "blocked"
    assert res.recovered is True
    assert calls == ["camilla:loopback", "fanin", "outputd", "hw-reconcile"]
    assert read_persisted_coupling(fanin_env) == "loopback"


def test_default_kick_targets_audio_hardware_reconcile_via_broker_start(monkeypatch):
    """Pin the default kick's broker contract: blocking ``start`` of the
    audio-hardware reconcile oneshot (mirrors output_topology_reset's kick)."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker

    seen: dict[str, object] = {}

    def fake_manage_units(*units, verb, reason, no_block, timeout):
        seen.update(units=units, verb=verb, no_block=no_block, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)

    ok, detail = cr._start_audio_hardware_reconcile(reason="t")

    assert ok is True and detail == ""
    assert seen["units"] == (cr.AUDIO_HARDWARE_RECONCILE_UNIT,)
    assert seen["verb"] == "start"
    assert seen["no_block"] is False
    assert seen["timeout"] == 15.0


_UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
# jts4 (Pi Zero 2 W), 2026-08-21, three sequential camilla restarts.
_MEASURED_CAMILLA_RESTART_SEC = (30.307, 28.675, 28.723)


def _unit_directives(name: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in (_UNIT_DIR / name).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "[")) or "=" not in text:
            continue
        key, _, value = text.partition("=")
        pairs.append((key, value))
    return pairs


def test_camilla_start_requeues_the_hardware_reconciler_by_construction():
    """The dependency that dominates the start bound is structural, not a fluke.

    jasper-camilla Requires= AND is After= the hardware reconciler, and that
    reconciler is a Type=oneshot whose RemainAfterExit is unset — so it is
    inactive between runs and every camilla start re-queues it in full.
    """
    import jasper.fanin.coupling_reconcile as cr

    camilla = _unit_directives("jasper-camilla.service")
    requires = {
        unit for key, value in camilla if key == "Requires" for unit in value.split()
    }
    after = {unit for key, value in camilla if key == "After" for unit in value.split()}
    assert cr.AUDIO_HARDWARE_RECONCILE_UNIT in requires
    assert cr.AUDIO_HARDWARE_RECONCILE_UNIT in after

    reconciler = dict(_unit_directives(cr.AUDIO_HARDWARE_RECONCILE_UNIT))
    assert reconciler["Type"] == "oneshot"
    assert reconciler.get("RemainAfterExit", "no") == "no"
    assert reconciler["TimeoutStartSec"] == (
        f"{int(cr._CAMILLA_REQUEUED_RECONCILE_START_SEC)}s"
    )

    # jasper-camilla declares no start override, so its own ceiling is the
    # manager default the constant mirrors.
    assert not [key for key, _ in camilla if key == "TimeoutStartSec"]


def test_camilla_start_bound_matches_its_enumerated_terms():
    """The bound is its terms, not a picked number — and it covers the metal."""
    import jasper.fanin.coupling_reconcile as cr

    assert cr._CAMILLA_START_TIMEOUT_SEC == (
        cr._CAMILLA_REQUEUED_RECONCILE_START_SEC
        + cr._CAMILLA_OWN_START_SEC
        + cr._DAEMON_OP_CLIENT_MARGIN_SEC
    )
    # The 8 s bound this shipped with was under every sample measured on jts4;
    # the derived bound clears all of them.
    assert max(_MEASURED_CAMILLA_RESTART_SEC) > 8.0
    assert cr._CAMILLA_START_TIMEOUT_SEC > max(_MEASURED_CAMILLA_RESTART_SEC)


def test_camilla_start_uses_the_derived_bound_through_the_broker(monkeypatch):
    """Pin the broker contract for the resume that was timing out."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker

    seen: dict[str, object] = {}

    def fake_manage_units(*units, verb, reason, no_block, timeout):
        seen.update(units=units, verb=verb, no_block=no_block, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)

    ok, detail = cr._start_camilla(reason="t")

    assert ok is True and detail == ""
    assert seen["units"] == (cr.CAMILLA_UNIT,)
    assert seen["verb"] == "start"
    assert seen["no_block"] is False
    assert seen["timeout"] == cr._CAMILLA_START_TIMEOUT_SEC


def test_camilla_stop_keeps_its_bound_because_a_stop_pulls_nothing(monkeypatch):
    """A stop cannot re-queue Requires=, so the start's dominant term is absent."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker

    seen: dict[str, object] = {}

    def fake_manage_units(*units, verb, reason, no_block, timeout):
        seen.update(units=units, verb=verb, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)
    cr._stop_camilla(reason="t")

    assert seen["verb"] == "stop"
    assert seen["timeout"] == 8.0
    assert seen["timeout"] < cr._CAMILLA_START_TIMEOUT_SEC


def test_coupling_auto_unit_ceiling_matches_the_derived_enumeration():
    """The shipped ceiling is the enumeration plus its stated headroom.

    The unit carried a hand-kept tally that drifted twice (#1252, #2175, #2285
    P7); it now cites this derivation instead, so this test is what keeps the
    two in step.
    """
    import jasper.fanin.coupling_reconcile as cr

    assert cr.COUPLING_AUTO_TIMEOUT_START_SEC == (
        cr.COUPLING_AUTO_ENUMERATED_WORST_SEC + cr._COUPLING_AUTO_CEILING_HEADROOM_SEC
    )
    assert cr._COUPLING_AUTO_CEILING_HEADROOM_SEC > 0

    shipped = dict(_unit_directives("jasper-fanin-coupling-auto.service"))
    assert shipped["TimeoutStartSec"] == str(int(cr.COUPLING_AUTO_TIMEOUT_START_SEC))


def test_coupling_auto_enumeration_carries_the_camilla_resume():
    """The enumerated pass contains the derived resume, not the old 8 s bound."""
    import jasper.fanin.coupling_reconcile as cr

    assert cr.COUPLING_AUTO_ENUMERATED_WORST_SEC > cr._CAMILLA_START_TIMEOUT_SEC
    # Re-deriving the pass with the old bound must land materially lower, which
    # is what makes the resume a load-bearing term rather than noise.
    saved = cr._CAMILLA_START_TIMEOUT_SEC
    try:
        cr._CAMILLA_START_TIMEOUT_SEC = 8.0
        with_old_bound = cr._coupling_auto_pass_ceiling_sec(broker_dead=False)
    finally:
        cr._CAMILLA_START_TIMEOUT_SEC = saved
    assert cr.COUPLING_AUTO_ENUMERATED_WORST_SEC - with_old_bound == (
        saved - 8.0
    )


def test_coupling_auto_ceiling_is_sized_for_a_live_broker():
    """The broker-dead figure is disclosed, never an input to the ceiling.

    A client bound shorter than a succeeding operation lies (the #2790 class);
    a ceiling that kills an already-degraded, unusually slow pass tells the
    truth. So the ceiling covers the broker-ALIVE legal worst and the
    broker-dead worst stays a disclosed residual.
    """
    import jasper.fanin.coupling_reconcile as cr

    assert cr.COUPLING_AUTO_BROKER_DEAD_WORST_SEC > (
        cr.COUPLING_AUTO_ENUMERATED_WORST_SEC
    )
    # Deliberately NOT covered — the gap is the disclosure, and it is real.
    assert cr.COUPLING_AUTO_TIMEOUT_START_SEC < (
        cr.COUPLING_AUTO_BROKER_DEAD_WORST_SEC
    )
    assert cr.COUPLING_AUTO_TIMEOUT_START_SEC > (
        cr.COUPLING_AUTO_ENUMERATED_WORST_SEC
    )


def test_daemon_op_ceiling_counts_the_preamble_and_names_the_retry():
    """The preamble is in the arithmetic; the root retry is the disclosed half.

    restart_broker converts a socket timeout to BrokerUnavailable and, as root
    (which this unit is), retries the same call through direct systemctl. That
    doubling is modelled so it can be disclosed, not so it can be budgeted.
    """
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker

    assert cr._BROKER_SOCKET_MARGIN_SEC == restart_broker._CLIENT_SOCKET_MARGIN_SEC
    # Broker alive: one attempt, plus the socket margin, plus any preamble.
    assert cr._daemon_op_ceiling_sec(10.0, reset_failed=False) == 15.0
    assert cr._daemon_op_ceiling_sec(10.0, reset_failed=True) == 25.0
    # Broker dead: the same call again through direct systemctl.
    assert cr._daemon_op_ceiling_sec(10.0, reset_failed=False, broker_dead=True) == 25.0
    assert cr._daemon_op_ceiling_sec(10.0, reset_failed=True, broker_dead=True) == 40.0
    # A start verb on a crash-budget unit is what earns the preamble.
    assert cr.CAMILLA_UNIT in cr._CRASH_BUDGET_UNITS
    assert "start" in cr._START_BUDGET_VERBS
    assert "stop" not in cr._START_BUDGET_VERBS


def test_audio_hardware_reconcile_unit_is_broker_start_permitted():
    """Guard the allowlist lockstep (the jts 2026-06-27 fan-in class): the unit
    the disarm kick starts must stay ``start``-permitted in the broker."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker

    assert restart_broker._unit_allowed_for_verb(
        cr.AUDIO_HARDWARE_RECONCILE_UNIT, "start"
    )


def test_shm_ring_cli_choices_accept_ring(tmp_path, monkeypatch, _ring_assets_present):
    # The CLI must accept shm_ring as a valid coupling argument.
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    captured = {}

    def fake_reconcile(coupling, **kw):
        captured["coupling"] = coupling
        from jasper.fanin.coupling_reconcile import CouplingResult

        return CouplingResult(ok=True, desired=coupling, changed=True, direction="arm")

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main(["shm_ring", "--no-apply"])
    assert rc == 0
    assert captured["coupling"] == "shm_ring"


# --- Blocker 2: shm_ring refused while the bond reads the dac_content lane -----


def test_arm_shm_ring_refused_for_active_leader_keeps_loopback(tmp_path):
    # BLOCKER 2: arming shm_ring on a box whose bond plays through outputd's
    # dac_content lane must be REFUSED before any daemon op — that lane pins
    # CONTENT_BRIDGE=direct, under which outputd reads the snd-aloop content PCM
    # an armed ring moves CamillaDSP off. Never touch the rings; keep loopback.
    # Reports the real desired coupling, not a hardcoded one.
    #
    # `active_leader_check` is the INJECTED route seam, which reports
    # leader-vs-solo only and cannot separate an active-speaker leader (lane
    # cleared) from a passive one (lane armed) — so it fails CLOSED, which is
    # what this test exercises. The narrowing is reached through the real
    # grouping-config reader instead; see
    # `test_arm_shm_ring_admitted_for_an_active_endpoint`.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )

    assert result.ok is False
    assert result.direction == "blocked"
    assert result.desired == COUPLING_SHM_RING
    assert result.changed is False
    assert calls == []  # no arm, no recovery ops needed (was already loopback)
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None


def _bonded_follower_cfg():
    from jasper.multiroom.config import GroupingConfig

    return GroupingConfig(
        enabled=True, role="follower", channel="right", bond_id="b",
        leader_addr="jts.local", buffer_ms=400, codec="flac", error=None,
    )


def _drive_grouping_shape(monkeypatch, *, box_is_active: bool, flat_allowed: bool):
    """Drive the reconciler's route shape through the REAL readers.

    The gate consults the dac_content-lane writer now, so a duck-typed config
    stub no longer reaches it — a real GroupingConfig plus the topology state
    the writer's own caller reads is what decides the verdict.
    """
    import jasper.multiroom.reconcile as mr

    monkeypatch.setattr(
        "jasper.multiroom.config.load_config",
        lambda *a, **k: _bonded_follower_cfg(),
        raising=False,
    )
    monkeypatch.setattr(
        mr, "_output_topology_state", lambda: (box_is_active, flat_allowed)
    )


def test_arm_shm_ring_refused_for_a_dumb_member(tmp_path, monkeypatch):
    # The block covers a FOLLOWER too (not just leader). Its subject is the
    # dac_content lane: a PASSIVE box with a saved flat-capable layout plays the
    # bond through that lane, which reads the snd-aloop content PCM an armed ring
    # moves CamillaDSP off.
    _drive_grouping_shape(monkeypatch, box_is_active=False, flat_allowed=True)
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        # active_leader_check omitted -> falls through to the grouping-config reader.
    )

    assert result.ok is False
    assert result.direction == "blocked"
    assert result.desired == COUPLING_SHM_RING
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_arm_shm_ring_admitted_for_an_active_endpoint(tmp_path, monkeypatch):
    """The narrowing at the reconciler: an ACTIVE endpoint's dac_content lane is
    cleared by the same writer, so the ring is no longer force-reverted here.

    Without this the reconciler would disarm a bonded active leader's ring on
    every boot and deploy, which is exactly the box the hardware pass needs
    armed.
    """
    _drive_grouping_shape(monkeypatch, box_is_active=True, flat_allowed=False)
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    # The ROUTE gate let it through. (It still meets the ordinary arm
    # preflights afterwards — this container has no ring platform installed —
    # so the assertion is scoped to the gate under test, not to a successful
    # arm.)
    assert result.direction != "blocked", result.detail
    assert "dac_content" not in (result.detail or "")


def test_ring_armed_box_that_becomes_grouped_recovers_to_loopback(
    tmp_path, _ring_assets_present
):
    # A box armed to shm_ring while solo, then grouping is enabled: a re-reconcile
    # of shm_ring must REVERT to loopback + direct (clearing Ring B keys), not
    # leave the leader stranded on a ring outputd can't feed coherently.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_SINK=dual_apple\n")
    calls, ro, rf, rc = _recorder()

    # 1) Arm shm_ring while solo (succeeds).
    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert (
        read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
        == "shm_ring"
    )

    # 2) Now bonded: the next shm_ring reconcile is refused and recovers.
    calls.clear()
    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )

    assert result.ok is False
    assert result.direction == "blocked"
    assert result.recovered is True
    assert result.desired == COUPLING_SHM_RING
    # Recovery ran the loopback disarm spine and cleared the ring bridge keys.
    assert "camilla:loopback" in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    outputd_text = outputd_env.read_text()
    assert read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) is None
    assert "JASPER_OUTPUTD_SHM_RING" not in outputd_text
    # The operator's own outputd line survives the recovery.
    assert "JASPER_OUTPUTD_SINK=dual_apple" in outputd_text


# ---------------------------------------------------------------------------
# CamillaDSP-coordinated fan-in restart (RTTIME-SIGKILL fix).
#
# While a fan-in-written ring/pipe coupling is live, a bare fan-in *process*
# restart detaches the ring writer; camilladsp's ioplug capture reader busy-spins
# and the SCHED_FIFO+LimitRTTIME unit is hard SIGKILLed ~213 ms later, cascading
# into OnFailure=jasper-camilla-recover. _restart_fanin_coordinated pauses camilla
# (clean SIGTERM) first and resumes it after fan-in is back — mirroring the
# fan-in -> camilla order jasper-camilla-recover proves works.
# ---------------------------------------------------------------------------


def _coord_ops(*, fanin_ok=True, stop_ok=True, start_ok=True):
    """Recording (calls, do_restart, do_stop_camilla, do_start_camilla) hooks."""
    calls: list[str] = []

    def do_restart() -> tuple[bool, str]:
        calls.append("fanin")
        return (fanin_ok, "" if fanin_ok else "fanin restart failed")

    def do_stop_camilla() -> tuple[bool, str]:
        calls.append("camilla_stop")
        return (stop_ok, "" if stop_ok else "camilla stop failed")

    def do_start_camilla() -> tuple[bool, str]:
        calls.append("camilla_start")
        return (start_ok, "" if start_ok else "camilla start failed")

    return calls, do_restart, do_stop_camilla, do_start_camilla


def _coordinated(coupling, calls_ops, **kw):
    from jasper.fanin.coupling_reconcile import _restart_fanin_coordinated

    _calls, do_restart, do_stop, do_start = calls_ops
    return _restart_fanin_coordinated(
        do_restart,
        do_stop,
        do_start,
        coupling=coupling,
        reason="t",
        phase="test",
        **kw,
    )


def test_coordinated_loopback_is_a_plain_restart_no_camilla_pause():
    """On loopback the snd-aloop buffer decouples fan-in from camilla, so the
    coordination is skipped: a single plain fan-in restart, NO camilla stop/start."""
    ops = _coord_ops()
    calls = ops[0]
    r = _coordinated(COUPLING_LOOPBACK, ops)
    assert calls == ["fanin"]
    assert r.ok is True
    assert r.fanin_restarted is True
    assert r.coordinated is False
    assert r.camilla_stopped is False and r.camilla_started is False


@pytest.mark.parametrize("coupling", [COUPLING_SHM_RING])
def test_coordinated_ring_pauses_before_and_resumes_after(coupling):
    """The load-bearing order: camilla is STOPPED before the fan-in restart and
    STARTED after it — exactly the fan-in -> camilla order the recover script proves."""
    ops = _coord_ops()
    calls = ops[0]
    r = _coordinated(coupling, ops)
    assert calls == ["camilla_stop", "fanin", "camilla_start"]
    assert r.ok is True
    assert r.coordinated is True
    assert r.fanin_restarted is True
    assert r.camilla_stopped is True and r.camilla_started is True


def test_coordinated_stop_failure_aborts_fanin_restart_and_keeps_camilla_running():
    """Safe direction #1: if camilla can't be paused it may still be on the ring, so
    we must NOT restart fan-in (that is what SIGKILLs it). Abort, surface ok=False,
    and ensure camilla is running (start-back) — never leave it stopped-forever."""
    ops = _coord_ops(stop_ok=False)
    calls = ops[0]
    r = _coordinated(COUPLING_SHM_RING, ops)
    # fan-in was NOT restarted; camilla start-back WAS attempted after the failed stop.
    assert "fanin" not in calls
    assert calls == ["camilla_stop", "camilla_start"]
    assert r.ok is False
    assert r.fanin_restarted is False
    assert r.camilla_stopped is False
    assert r.camilla_started is True
    assert "aborted fan-in restart" in r.detail


def test_coordinated_fanin_failure_still_resumes_camilla():
    """Safe direction #2: if the fan-in restart fails AFTER camilla was stopped, we
    STILL start camilla back — never leave the DSP stopped-forever."""
    ops = _coord_ops(fanin_ok=False)
    calls = ops[0]
    r = _coordinated(COUPLING_SHM_RING, ops)
    # camilla was resumed even though fan-in failed.
    assert calls == ["camilla_stop", "fanin", "camilla_start"]
    assert r.ok is False
    assert r.fanin_restarted is False
    assert r.camilla_started is True
    assert "fan-in restart failed" in r.detail


def test_coordinated_resume_failure_is_surfaced():
    """A failed camilla RESUME is surfaced (ok=False, detail) so the operator/doctor
    sees the DSP is down; OnFailure=jasper-camilla-recover remains the backstop."""
    ops = _coord_ops(start_ok=False)
    r = _coordinated(COUPLING_SHM_RING, ops)
    assert r.ok is False
    assert r.fanin_restarted is True
    assert r.camilla_started is False
    assert "camilla resume failed" in r.detail


def test_camilla_stop_start_are_broker_authorized():
    """Contract pin (AGENTS.md 'pin promises with tests'): the coordinated restart
    stops+starts jasper-camilla through the broker, so pin that the broker + polkit
    already authorize exactly that. If someone drops jasper-camilla from MANAGED_UNITS
    or removes the stop/start verbs, THIS fails loudly instead of silently re-opening
    the RTTIME-SIGKILL cascade on jts.local. No NEW grant was needed for this PR."""
    from jasper.control import restart_broker as rb

    assert "jasper-camilla.service" in rb.MANAGED_UNITS
    assert "stop" in rb.ALLOWED_VERBS
    assert "start" in rb.ALLOWED_VERBS
    assert rb._unit_allowed_for_verb("jasper-camilla.service", "stop") is True
    assert rb._unit_allowed_for_verb("jasper-camilla.service", "start") is True


# --- #2175: a config-apply restart must not spend the crash-reboot budget ----


class _FakeSystemd:
    """The slice of PID 1 this defect lives in: a per-unit start rate limit.

    Models ``StartLimitIntervalSec`` / ``StartLimitBurst`` / ``StartLimitAction``
    as systemd implements them for the units this reconciler bounces: every
    start-consuming action spends a slot, ``reset-failed`` zeroes the counter,
    and exhausting the burst inside the window fires the action (for
    jasper-fanin: reboot). Numbers are read from the shipped unit so a retune
    of the real budget retunes this reproduction with it.
    """

    def __init__(self, unit: str, *, burst: int) -> None:
        self.unit = unit
        self.burst = burst
        self.starts = 0
        self.rebooted = False

    def manage_units(self, *units, verb="restart", reason="", no_block=True,
                     timeout=5.0):
        for name in units:
            if name != self.unit:
                continue
            if verb == "reset-failed":
                self.starts = 0
            elif verb in {"start", "restart", "try-restart"}:
                self.starts += 1
                if self.starts > self.burst:
                    self.rebooted = True
                    return {"ok": False, "error": "start request repeated too quickly"}
        return {"ok": True}


def _fanin_start_limit() -> tuple[int, str]:
    from tests.systemd_unit_helpers import value_for

    unit = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "systemd" / "jasper-fanin.service"
    ).read_text(encoding="utf-8")
    return int(value_for(unit, "StartLimitBurst")), value_for(unit, "StartLimitAction")


def test_repeated_config_applies_never_reach_fanin_start_limit_reboot(monkeypatch):
    """#2175 reproduction: a household member toggling a source must never
    reboot the speaker.

    Every source transaction asks this owner to converge, and a desired-On USB
    source that cannot compose re-arms/disarms fan-in on each pass — so a burst
    of toggles becomes a burst of DELIBERATE fan-in restarts. Against the real
    unit's budget (StartLimitBurst inside StartLimitIntervalSec, escalating to
    StartLimitAction=reboot) that is exactly what rebooted a Zero 2 W. The
    reset-failed that precedes each deliberate restart is what keeps the count
    from accumulating."""
    import jasper.fanin.coupling_reconcile as cr

    burst, action = _fanin_start_limit()
    assert action == "reboot", (
        "this test's premise is jasper-fanin's reboot escalation; if the unit "
        f"no longer declares StartLimitAction=reboot, revisit it (got {action!r})"
    )
    fake = _FakeSystemd(cr.FANIN_UNIT, burst=burst)
    monkeypatch.setattr("jasper.control.restart_broker.manage_units",
                        fake.manage_units)

    for _ in range(burst * 2):
        ok, detail = cr._restart_fanin(reason="source enable/disable")
        assert ok is True, detail

    assert fake.rebooted is False
    assert fake.starts == 1  # each apply resets, then spends exactly one slot


def test_fake_systemd_models_start_limit_escalation():
    """POSITIVE CONTROL for the fixture, NOT evidence about production code.

    This exercises only ``_FakeSystemd`` — no reconciler code runs — so it
    proves exactly one thing: the fake CAN reach ``rebooted``, which is what
    makes the ``assert fake.rebooted is False`` above a real assertion instead
    of a vacuous one. It documents the modelled budget (start-consuming verbs
    spend a slot; exceeding the burst fires the action) and would stay green if
    the production guard were reverted.

    The production-scope evidence lives in
    :func:`test_start_budget_reset_covers_only_crash_budget_daemon_starts`,
    which pins that the reset precedes a start-consuming verb on a crash-budget
    daemon and nothing else.
    """
    import jasper.fanin.coupling_reconcile as cr

    burst, _action = _fanin_start_limit()
    fake = _FakeSystemd(cr.FANIN_UNIT, burst=burst)

    for _ in range(burst + 1):  # systemd restarting the unit on its own
        fake.manage_units(cr.FANIN_UNIT, verb="restart")

    assert fake.rebooted is True


def test_start_budget_reset_covers_only_crash_budget_daemon_starts(monkeypatch):
    """Scope pin. The reset precedes a start-consuming verb on a long-running
    daemon that carries a crash budget — not a stop (which spends no start
    slot), and not the oneshot owners this module kicks (they have no crash
    budget, and are START_ONLY in the broker, which denies ``reset-failed``)."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker as rb

    calls: list[tuple[str, str]] = []

    def fake_manage(*units, verb="restart", reason="", no_block=True, timeout=5.0):
        calls.append((units[0], verb))
        return {"ok": True}

    monkeypatch.setattr("jasper.control.restart_broker.manage_units", fake_manage)

    cr._restart_outputd(reason="t")
    cr._stop_camilla(reason="t")
    cr._start_camilla(reason="t")
    cr._start_audio_hardware_reconcile(reason="t")

    assert calls == [
        (cr.OUTPUTD_UNIT, "reset-failed"),
        (cr.OUTPUTD_UNIT, "restart"),
        (cr.CAMILLA_UNIT, "stop"),
        (cr.CAMILLA_UNIT, "reset-failed"),
        (cr.CAMILLA_UNIT, "start"),
        (cr.AUDIO_HARDWARE_RECONCILE_UNIT, "start"),
    ]
    # Why the oneshot is excluded rather than merely unnecessary.
    assert rb._unit_allowed_for_verb(cr.AUDIO_HARDWARE_RECONCILE_UNIT,
                                     "reset-failed") is False


def test_start_budget_reset_is_best_effort_and_never_blocks_the_restart(
    monkeypatch, caplog,
):
    """A denied or failed reset is logged and the restart still runs: the reset
    is defence for the reboot budget, never a new gate in front of the audio
    graph's recovery path."""
    import jasper.fanin.coupling_reconcile as cr

    calls: list[str] = []

    def fake_manage(*units, verb="restart", reason="", no_block=True, timeout=5.0):
        calls.append(verb)
        if verb == "reset-failed":
            return {"ok": False, "error": "denied"}
        return {"ok": True}

    monkeypatch.setattr("jasper.control.restart_broker.manage_units", fake_manage)

    with caplog.at_level("WARNING"):
        ok, detail = cr._restart_fanin(reason="t")

    assert ok is True and detail == ""
    assert calls == ["reset-failed", "restart"]
    assert "result=start_budget_reset_failed" in caplog.text


def test_crash_budget_units_are_broker_reset_failed_permitted():
    """Allowlist lockstep (the jts 2026-06-27 fan-in class): every unit whose
    start budget this module clears must stay ``reset-failed``-permitted in the
    broker, or the defence silently degrades to a warning per restart."""
    import jasper.fanin.coupling_reconcile as cr
    from jasper.control import restart_broker as rb

    assert "reset-failed" in rb.ALLOWED_VERBS
    for unit in cr._CRASH_BUDGET_UNITS:
        assert rb._unit_allowed_for_verb(unit, "reset-failed") is True, unit


# --- removed transport_pipe migration (fail-safe to loopback) ----------------


def test_reconcile_auto_removed_coupling_fails_safe_ignoring_operator_marker(
    tmp_path, caplog
):
    """A persisted REMOVED coupling value (the deleted transport_pipe, or a typo) is
    NOT a valid operator choice — the mode the operator picked no longer exists. The
    --auto pass converges the box to loopback LOUDLY, IGNORING the operator marker,
    so a migrating box never silently keeps a deleted mode."""
    import logging

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}=transport_pipe\nJASPER_FANIN_COUPLING_CHOICE=operator\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    with caplog.at_level(logging.WARNING, logger="jasper.fanin.coupling_reconcile"):
        result = reconcile_auto(
            reason="t",
            env_path=fanin_env,
            outputd_env_path=outputd_env,
            gadget_present=False,
            usb_intent_enabled=False,
            restart_fanin=rf,
            restart_outputd=ro,
            reconcile_camilla=rc,
            kick_hardware_reconcile=lambda: (True, ""),
            active_leader_check=lambda: False,
        )

    # The operator marker was IGNORED because the persisted mode is removed.
    assert result.owned is True
    assert result.coupling == COUPLING_LOOPBACK
    assert "failed safe to loopback" in result.reason
    # fanin.env now names loopback — the file stops lying about a deleted mode.
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    # Loud WARNING event so the migration is never silent.
    assert "removed_coupling_failsafe" in caplog.text


def test_outputd_actions_unset_legacy_local_content_pipe(tmp_path):
    """Both the loopback AND shm_ring outputd-action branches sweep the legacy
    JASPER_OUTPUTD_LOCAL_CONTENT_PIPE key (the removed transport_pipe coupling's
    outputd content source) so a migrating box converges clean."""
    for coupling in (COUPLING_LOOPBACK, COUPLING_SHM_RING):
        actions = _outputd_actions(coupling, "")
        sweeps = [
            a
            for a in actions
            if a.action == "unset" and a.key == _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV
        ]
        assert sweeps, (
            f"{coupling} branch must unset {_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV}"
        )
    assert _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV == "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE"



# ---------------------------------------------------------------------------
# The ASSISTANT-WIDTH transient (U2 PR-2, item 1e).
#
# jasper-voice resolves the box's assistant wire width ONCE at start and is not
# part of the ordered audio-graph bounce. A coupling flip can change that width,
# so without a restart the box would sit in a standing width disagreement —
# converted losslessly and logged, but permanent. These pin that the reconciler
# ends it, that it does so only on an actual transition, and that it cannot
# start a unit that was deliberately stopped.
# ---------------------------------------------------------------------------


@pytest.fixture
def _wide_arm_gates_pass(monkeypatch, _ring_assets_present):
    """Assets + geometry (from `_ring_assets_present`) plus the two WIDTH gates.

    A declared-WIDE box agrees with the shipped conf.d (it now spells `format
    S32_LE` explicitly), but still needs `ring_wire_caps_ready` — a dev host
    carries no ioplug provenance record. An OPERATOR-NARROW-PINNED box is the
    opposite shape: it disagrees with that same unconditionally-wide shipped
    conf.d, so `ring_edge_width_ready` genuinely refuses it there (correctly —
    see the dedicated ``test_ring_edge_width_ready_*`` tests, which build their
    own conf.d rather than use this fixture). Both refusals are real and both
    have their own tests; stubbing the two gates here keeps THESE tests about
    the assistant-width TRANSITION — narrow-declared or wide-declared — instead
    of re-testing the arm preflight.
    """
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr(
        cr, "ring_edge_width_ready", lambda **kw: (True, "")
    )
    monkeypatch.setattr(cr, "ring_wire_caps_ready", lambda **kw: (True, ""))


def _wide_declared_env(tmp_path, coupling: str) -> Path:
    """A fanin.env declaring the wide wire format at the given coupling."""
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR, RING_WIRE_FORMAT_WIDE

    return _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={coupling}\n"
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT_WIDE}\n",
    )


def test_arming_a_declared_wide_box_restarts_voice_once(
    tmp_path, _wide_arm_gates_pass
):
    """narrow -> wide: the flip changes the assistant width, so voice re-reads it."""
    fanin_env = _wide_declared_env(tmp_path, COUPLING_LOOPBACK)
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert result.ok, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == ["voice"], (
        "a declared-wide box crossing into shm_ring changes the assistant width "
        "from S16_LE to S32_LE; voice resolves that once at start"
    )


def test_disarming_a_declared_wide_box_restarts_voice_once(tmp_path):
    """wide -> narrow is equally a transition, and equally standing if unhandled."""
    fanin_env = _wide_declared_env(tmp_path, COUPLING_SHM_RING)
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert calls == ["voice"]


def test_a_narrow_box_flipping_coupling_does_not_restart_voice(
    tmp_path, _wide_arm_gates_pass
):
    """THE PRECISION OF THE TRIGGER, on the box that still demonstrates it.

    An OPERATOR-NARROW-PINNED box (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) is
    narrow on BOTH sides of a loopback -> shm_ring flip, so restarting voice there
    would cut a reply for nothing — and would make this a per-flip bounce rather
    than a per-transition one. This is no longer "almost every box in the
    fleet": since the ring wire resolver's default went wide, an UNDECLARED box
    genuinely changes width across this same flip — see
    ``test_an_undeclared_box_flipping_coupling_does_restart_voice`` right below,
    the complement this test used to (wrongly) claim to cover.

    ``_wide_arm_gates_pass`` bypasses `ring_edge_width_ready`/`ring_wire_caps_ready`
    despite its name — the two gates it stubs are coupling/width-agnostic, and a
    narrow pin disagrees with the shipped (unconditionally wide) conf.d
    `_ring_assets_present` points at, which would refuse the arm for a reason
    unrelated to what this test is about.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_LOOPBACK}\n"
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == [], "the assistant width never moved; voice had nothing to re-read"


def test_an_undeclared_box_flipping_coupling_does_restart_voice(
    tmp_path, _ring_assets_present
):
    """THE FLIP'S CONSEQUENCE, from the other side of the pair above.

    Every box that has not pinned itself narrow resolves the ring wire resolver's
    WIDE default (`resolve_ring_wire_format`), so an undeclared box crossing
    loopback -> shm_ring moves BOTH halves of `assistant_wire_is_wide`'s
    conjunction (coupling AND format) to true — the width genuinely changes, so
    voice must be told. `_ring_assets_present` alone (no width-gate bypass) is
    enough here: the shipped conf.d spells S32_LE explicitly, matching what this
    undeclared box resolves, so `ring_edge_width_ready`/`ring_wire_caps_ready`
    pass on their own merits rather than needing to be stubbed out of the way.
    """
    fanin_env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_LOOPBACK}\n")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        active_leader_check=lambda: False,
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert result.ok, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == ["voice"], (
        "an undeclared box's assistant width moves from S16_LE to S32_LE across "
        "this flip; voice resolves that once at start"
    )


def test_a_confirm_pass_on_an_armed_wide_box_does_not_restart_voice(
    tmp_path, _wide_arm_gates_pass
):
    """The reconciler runs on boot, deploy and every source transaction.

    A confirm pass moves nothing, so it must not bounce voice — otherwise every
    /sources/ toggle on an armed wide box would cut the assistant off.
    """
    fanin_env = _wide_declared_env(tmp_path, COUPLING_SHM_RING)
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert calls == []


def test_a_staging_write_never_restarts_voice(tmp_path):
    """``apply=False`` writes the env and runs NO daemon ops. Voice included."""
    fanin_env = _wide_declared_env(tmp_path, COUPLING_LOOPBACK)
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == [], "a staging pass performing one daemon op is not a staging pass"


def test_a_failed_voice_restart_does_not_change_the_coupling_verdict(
    tmp_path, _wide_arm_gates_pass
):
    """Best-effort, and deliberately so.

    The coupling IS reconciled either way; the remaining exposure is a width
    disagreement the reader converts losslessly. Failing the reconcile over it
    would trade a precision difference for an audio-path outage.
    """
    fanin_env = _wide_declared_env(tmp_path, COUPLING_LOOPBACK)
    outputd_env = _write(tmp_path / "outputd.env", "")

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        restart_voice=lambda: (False, "broker refused"),
    )

    assert result.ok, "a best-effort voice restart must not fail the coupling"
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_voice_is_try_restarted_so_a_stopped_unit_stays_stopped(monkeypatch):
    """THE VERB IS THE SAFETY PROPERTY.

    ``restart`` would START jasper-voice on a box where it is deliberately down
    — a no-mic box parks it through
    ``ConditionPathExists=!/var/lib/jasper/voice-input-absent``, and an operator
    can stop it. A coupling flip is not permission to start either one.
    ``try-restart`` is a no-op on an inactive unit.
    """
    import jasper.fanin.coupling_reconcile as cr

    seen = {}

    def _manage_units(unit, *, verb, reason, no_block, timeout):
        seen["unit"] = unit
        seen["verb"] = verb
        return {"ok": True}

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units", _manage_units
    )
    ok, _detail = cr._try_restart_voice(reason="t")
    assert ok
    assert seen["unit"] == cr.VOICE_UNIT == "jasper-voice.service"
    assert seen["verb"] == "try-restart", (
        "restart would start a parked or deliberately-stopped jasper-voice"
    )
    # And it is NOT in the crash-budget reset set: this fires once per coupling
    # flip, so it cannot walk the start-limit window the per-transaction fan-in
    # bounces could. A reset-failed here would be the only call before it.
    assert cr.VOICE_UNIT not in cr._CRASH_BUDGET_UNITS


def test_the_default_voice_restart_wiring_issues_try_restart(
    tmp_path, monkeypatch, _wide_arm_gates_pass
):
    """THE JOIN, not the pieces.

    `test_voice_is_try_restarted_so_a_stopped_unit_stays_stopped` pins
    `_try_restart_voice`'s verb, and the transition tests pin WHEN a restart is
    issued — but both sides could be right while the wire between them is wrong,
    because every one of those tests supplies its own `restart_voice`. Mutating
    the default lambda's verb to ``restart`` left the whole suite green.

    So this one calls `reconcile_coupling` with `restart_voice` DELIBERATELY not
    injected, and stubs one layer lower — at the restart broker — so the real
    `restart_voice or (lambda: _try_restart_voice(reason=reason))` join is the
    code under test. Everything else is still injected, which is what makes the
    single recorded broker call unambiguous: if the join were wired to any other
    helper, or to `restart`, this fails.
    """
    fanin_env = _wide_declared_env(tmp_path, COUPLING_LOOPBACK)
    outputd_env = _write(tmp_path / "outputd.env", "")
    broker_calls = []

    def _manage_units(unit, *, verb, reason, no_block, timeout):
        broker_calls.append((unit, verb))
        return {"ok": True}

    monkeypatch.setattr("jasper.control.restart_broker.manage_units", _manage_units)

    result = reconcile_coupling(
        COUPLING_SHM_RING,
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        # Everything EXCEPT restart_voice, so the only broker traffic this test
        # can observe is the join it is pinning.
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        kick_hardware_reconcile=lambda: (True, ""),
    )

    assert result.ok, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    voice_calls = [c for c in broker_calls if c[0] == "jasper-voice.service"]
    assert voice_calls == [("jasper-voice.service", "try-restart")], (
        "the default wiring must reach the broker exactly once, as a "
        f"try-restart of jasper-voice; got {broker_calls}"
    )
    # A successful coupling change also re-bakes a bonded ACTIVE leader's
    # camilla#1, whose capture device was baked at BOND time: nothing else
    # re-derives it on a flip and the two units are unordered, so without this
    # an arm leaves camilla#1 on the tap the ring just took fan-in off — a
    # silent bond with every daemon healthy. No-op on a solo box.
    assert ("jasper-grouping-reconcile.service", "start") in broker_calls


def test_the_default_voice_restart_wiring_is_not_reached_without_a_transition(
    tmp_path, monkeypatch, _wide_arm_gates_pass
):
    """The same join, from the other side.

    Without this, a default wired to fire unconditionally would still satisfy
    the test above. Same setup, same absent injection, an OPERATOR-NARROW-PINNED
    box this time (see ``test_a_narrow_box_flipping_coupling_does_not_restart_voice``
    for why an undeclared box no longer demonstrates "narrow on both sides"): the
    broker must see nothing at all.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_LOOPBACK}\n"
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    broker_calls = []

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda unit, **kw: broker_calls.append((unit, kw.get("verb"))) or {"ok": True},
    )

    reconcile_coupling(
        COUPLING_SHM_RING,
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda _c: (True, ""),
        kick_hardware_reconcile=lambda: (True, ""),
    )

    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    voice_calls = [c for c in broker_calls if c[0] == "jasper-voice.service"]
    assert voice_calls == [], (
        f"a narrow box's assistant width never moved; got {broker_calls}"
    )
