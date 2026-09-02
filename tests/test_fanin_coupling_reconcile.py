# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered convergence of the fan-in -> CamillaDSP ring coupling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# The conf.d this repo actually installs — the honest "box on the shipped wire"
# fixture for the gates that read both PCM blocks.
SHIPPED_RING_CONF_D = (
    Path(__file__).resolve().parents[1] / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)

from jasper.env_file import read_value
from jasper.fanin.coupling_reconcile import (
    _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV,
    _outputd_actions,
    default_ring_gates,
    reconcile_coupling,
)
from jasper.fanin.ring_health import (
    FANIN_ENV_PATH,
    OUTPUTD_ENV_PATH,
    persisted_coupling_feeds_ring,
    read_persisted_coupling,
    ring_edge_width_ready,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
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
    monkeypatch.setattr("jasper.fanin.ring_health.JASPER_ENV_PATH", str(jasper_env))
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
    monkeypatch.setattr("jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env))
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

    def reconcile_camilla() -> tuple[bool, str]:
        calls.append(f"camilla:{COUPLING_SHM_RING}")
        ok = camilla_ok and camilla_fail_for is None
        return (ok, "reconciled" if ok else "invalid config")

    return calls, restart_outputd, restart_fanin, reconcile_camilla


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _reconcile(
    *,
    fanin_env: Path,
    outputd_env: Path,
    restart_outputd,
    restart_fanin,
    reconcile_camilla,
    **kwargs,
):
    # Keep tests hermetic: never let the default hardware-reconcile kick reach
    # the real restart broker. Kick-behaviour tests inject their own.
    kwargs.setdefault("kick_hardware_reconcile", lambda: (True, ""))
    # Same for the assistant-width voice restart: it fires only on a width
    # TRANSITION, so most tests never reach it, but a test that flips a
    # declared-wide box's coupling would otherwise drive the real broker.
    kwargs.setdefault("restart_voice", lambda: (True, ""))
    return reconcile_coupling(
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_outputd=restart_outputd,
        restart_fanin=restart_fanin,
        reconcile_camilla=reconcile_camilla,
        **kwargs,
    )


def test_no_apply_writes_the_ring_env_and_bounces_nothing(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.changed and calls == []
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_convergence_preserves_coexisting_keys_and_custom_outputd_ring_path(
    tmp_path, _ring_assets_present
):
    fanin_env = _write(
        tmp_path / "fanin.env",
        "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096\n# operator note\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        f"{OUTPUTD_RING_PATH_ENV_VAR}=/run/custom/content.ring\n",
    )
    _, ro, rf, rc = _recorder()

    _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    fanin_body = fanin_env.read_text(encoding="utf-8")
    outputd_body = outputd_env.read_text(encoding="utf-8")
    assert "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096" in fanin_body
    assert "# operator note" in fanin_body
    assert "JASPER_CAMILLA_CHUNKSIZE=256" in outputd_body
    # The convergence preserves the operator's custom Ring B path.
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok is False and res.changed is False and calls == []


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
        return cr.CouplingResult(ok=True, changed=False)

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main([COUPLING_SHM_RING])
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
        lambda *a, **k: cr.CouplingResult(ok=True, changed=False),
    )
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    # Simulate the fresh CLI interpreter: no root handler yet. (Under pytest the
    # capture plugin's own root handlers would make basicConfig a silent no-op.)
    root.handlers.clear()
    try:
        assert cr.main([COUPLING_SHM_RING]) == 0
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
        reason="source steady state",
        force=False,
    ) == (True, "unchanged")
    assert observed == [{"force": False}]


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
    ok, detail = cr._reconcile_camilla(**rung_kwargs)
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
    ok, detail = cr._reconcile_camilla(reason="confirm", force=False)

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
    return cr._reconcile_camilla(reason="arm", force=True)


def _staged_anchor_skip(transport: str) -> dict:
    from jasper.fanin.coupling_reconcile import CARRIER_TRANSIENT_ACTIVE_REFUSAL

    return {
        "status": "skipped",
        "reason": CARRIER_TRANSIENT_ACTIVE_REFUSAL,
        "transport": transport,
        "current_config_path": "/var/lib/camilladsp/configs/"
        "active_speaker_staged_startup.yml",
    }


def test_the_camilla_rung_will_not_claim_a_converged_anchor_off_the_statefile(
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


def test_the_camilla_rung_still_accepts_the_anchor_over_the_websocket(monkeypatch):
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
            gadget_present=True,
            usb_combo_changed=True,
            reason="USB combo resolved from canonical source intent",
        )

    monkeypatch.setattr(cr, "reconcile_auto", fake_auto)
    rc = cr.main(["--auto"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "coupling auto:" in out and "ok=True" in out


def test_cli_auto_and_explicit_are_mutually_exclusive(monkeypatch):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        cr.main([COUPLING_SHM_RING, "--auto"])


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
            gadget_present=False,
            usb_combo_changed=False,
            reason="",
        )

    monkeypatch.setattr(cr, "reconcile_auto", fake_auto)

    rc = cr.main(["--auto"])

    assert rc == 0  # verb ran and succeeded despite no lock
    assert ran["auto"] == 1


@pytest.mark.parametrize("argv", [["--auto"], [COUPLING_SHM_RING]])
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
            gadget_present=False,
            usb_combo_changed=False,
            reason="",
        )

    def fake_reconcile(*a, **k):
        probe_lock()
        return cr.CouplingResult(
            ok=True,
            changed=False,
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
    """Force the shm_ring activation gates to pass (assets + wire capability).

    Tests about the ARM SPINE (order, camilla-failure rollback, disarm) stub
    ring-asset presence and the ioplug wire-capability record so they exercise
    the daemon path without a real conf.d/ioplug on the test host. The
    stale-ring-file guard is also stubbed to a no-op so the spine tests don't
    touch /dev/shm.

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


def test_convergence_writes_the_coherent_pair_in_order(tmp_path, _ring_assets_present):
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )
    assert result.ok
    # Ordered spine: outputd (Ring B reader) -> fanin (Ring A writer) -> camilla.
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    outputd_text = outputd_env.read_text()
    assert read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) == "shm_ring"
    assert read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR) == "2"


# --- D5 (wide-output-path program): ring wire-width preflight ----------------


def _break_ring_kwargs_override(monkeypatch, *, playback_format: str | None):
    """Simulate the shm_ring coupling losing its narrow-lane override.

    ``playback_format=None`` drops the key entirely (someone deleted the
    override, so the emitter falls back to the box-wide default); a string sets
    it to a width the ring cannot carry. Patches the module attribute
    ``content_lane_format_for_coupling`` actually calls."""
    import jasper.fanin_coupling as coupling

    real = coupling.capture_kwargs_for_coupling

    def broken():
        kwargs = dict(real())
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
    # Fails closed (ADR-0100), never a fallback — the reason code this gate
    # actually carries, not the English sentence around it.
    assert "ADR-0100" in detail


_FEEDS_RING_CASES = [
    ("", True),
    (f"{COUPLING_ENV_VAR}=\n", True),
    (f"{COUPLING_ENV_VAR}=   \n", True),
    (f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n", True),
    (f"{COUPLING_ENV_VAR}=loopback\n", False),
    (f"{COUPLING_ENV_VAR}=wat\n", False),
]
_FEEDS_RING_IDS = ["absent_key", "empty", "whitespace", "declared", "retired", "typo"]


@pytest.mark.parametrize("fanin_text,feeds_ring", _FEEDS_RING_CASES, ids=_FEEDS_RING_IDS)
def test_persisted_coupling_feeds_ring_answers_the_file_and_a_snapshot_alike(
    tmp_path, fanin_text, feeds_ring
):
    """The ONE predicate for "is fan-in filling Ring A", through both its doors.

    ADR-0100 left one transport: `jasper-fanin` serves an absent key, an empty
    value and the token alike and refuses anything else at parse (exit 78), so
    naming nothing IS a fan-in on the ring. The ``text`` door must reach the same
    verdict as the path door — a caller that already holds fanin.env's text is
    the reason the readers used to spell this rule for themselves (#3655).
    """
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(fanin_text, encoding="utf-8")

    assert persisted_coupling_feeds_ring(fanin_env) is feeds_ring
    assert persisted_coupling_feeds_ring(text=fanin_text) is feeds_ring


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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
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


def test_converge_refuses_the_spine_when_the_content_format_converge_fails(
    tmp_path, _ring_assets_present
):
    """Fail-CLOSED: no converge, no spine — and specifically, no outputd restart.

    Restarting outputd after a failed converge is exactly the reboot path above,
    so the refusal must land BEFORE the spine. There is nowhere to fall back to,
    so the box parks with a reason naming the key and the remedy.
    """
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        kick_hardware_reconcile=lambda: (False, "unit failed"),
    )

    assert result.ok is False
    assert "JASPER_OUTPUTD_CONTENT_FORMAT" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail
    # No daemon is bounced at all: the refusal lands ahead of the spine.
    assert calls == []
    # The persisted intent still names the ring — there is nowhere else to go,
    # and the box parks visibly rather than being walked onto a second route.
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_convergence_migrates_stale_ring_slots_then_converges(tmp_path, monkeypatch):
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    # The stale =8 line was overridden in fanin.env (the later systemd env file).
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


def test_convergence_overrides_stale_base_ring_slots_then_converges(tmp_path, monkeypatch):
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
    monkeypatch.setattr("jasper.fanin.ring_health.JASPER_ENV_PATH", str(jasper_env))

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"


def test_convergence_keeps_matching_operator_ring_slots(tmp_path, monkeypatch):
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert result.ok is True, result.detail
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    # The matching operator override is preserved.
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "4"


def test_convergence_deletes_a_stale_on_disk_ring_before_the_spine(tmp_path, monkeypatch):
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert result.ok is True, result.detail
    assert not program.exists(), "stale mismatched Ring A must be deleted before arm"
    assert content.exists(), "coherent Ring B must be left untouched"


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


def test_confirm_shm_ring_coherent_stays_lightweight(tmp_path, monkeypatch):
    # The other side of the CONFIRM-path fix: a COHERENT already-armed shm_ring box
    # must NOT bounce fan-in/outputd on every reconcile tick — only re-load camilla.
    # This pins that the escalation is gated on POSITIVE incoherence evidence, so a
    # healthy box keeps the cheap confirm rather than always running _converge_ring.
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert result.ok is True, result.detail
    assert not result.changed
    # Lightweight: camilla-only re-load, NO fan-in / outputd bounce.
    assert calls == ["camilla:shm_ring"]
    # The coherent operator override is preserved.
    assert read_value(fanin_env.read_text(), "JASPER_FANIN_RING_SLOTS") == "2"
    assert program.exists()
    assert content.exists()


# --- DEFECT 2: ring_topology_ready end-to-end over REAL on-disk topologies ----
# The tests above either exercise the topology_supports_shm_ring predicate in
# isolation (tests/test_runtime_contract_ring.py) or MOCK the predicate at the
# reconciler seam. Neither proves the actual arming gate the defect names
# (arm_ring_topology_ineligible) resolves a REAL topology loaded from disk — nor
# that a real stale-subwoofer topology honestly refuses through that same gate.
# These close that gap: an unconfigured draft must remain silent until a
# passive stereo layout is saved, while the stale-subwoofer refusal stays clear.


def test_ring_topology_ready_refuses_real_stale_subwoofer_with_reset_hint(
    tmp_path,
    monkeypatch,
):
    # The negative end-to-end path over a REAL topology (not a mocked predicate):
    # a plain Apple-dongle box whose SAVED topology still declares a subwoofer role
    # from the 2026-06 campaign refuses through ring_topology_ready() — a stereo
    # ring genuinely cannot drive a sub — and the refusal names the actionable
    # remediation (jasper-output-topology-reset) instead of an opaque "loopback".
    from jasper.fanin.ring_health import ring_topology_ready
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
# jts4 (Pi Zero 2 W), 2026-08-21, three sequential camilla restarts, anchored
# Stopping -> Started.
_MEASURED_CAMILLA_RESTART_SEC = (30.245, 28.621, 28.664)


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
        cr._CAMILLA_DEPENDENCY_CRITICAL_PATH_SEC
        + cr._CAMILLA_OWN_START_SEC
        + cr._DAEMON_OP_CLIENT_MARGIN_SEC
    )
    # The 8 s bound this shipped with was under every sample measured on jts4;
    # the derived bound clears all of them.
    assert max(_MEASURED_CAMILLA_RESTART_SEC) > 8.0
    assert cr._CAMILLA_START_TIMEOUT_SEC > max(_MEASURED_CAMILLA_RESTART_SEC)


def test_camilla_dependency_path_follows_the_shipped_ordering_edges():
    """The dependency term is the critical path, and the edges decide it.

    All three units camilla pulls are terms — an earlier revision excluded fan-in
    and outputd on a premise that is false three ways in this file. They are not
    simply max()'d either: jasper-audio-hardware-reconcile declares
    ``Before=jasper-outputd.service``, so those two run in series while fan-in
    runs alongside. Read the edges from the shipped units so an added ordering
    edge that lengthens the path fails here instead of under-bounding the call.
    """
    import jasper.fanin.coupling_reconcile as cr

    camilla = _unit_directives("jasper-camilla.service")
    pulled = {
        unit
        for key, value in camilla
        if key in {"Requires", "Wants"}
        for unit in value.split()
        if unit.endswith(".service")
    }
    after = {unit for key, value in camilla if key == "After" for unit in value.split()}
    deps = pulled & after
    assert deps == {
        cr.AUDIO_HARDWARE_RECONCILE_UNIT,
        cr.FANIN_UNIT,
        cr.OUTPUTD_UNIT,
    }

    def before_of(unit: str) -> set[str]:
        return {
            u
            for key, value in _unit_directives(unit)
            if key == "Before"
            for u in value.split()
        }

    # The serialising edge the critical path rests on.
    assert cr.OUTPUTD_UNIT in before_of(cr.AUDIO_HARDWARE_RECONCILE_UNIT)
    # Fan-in orders nothing against the other two, so it runs in parallel.
    assert not before_of(cr.FANIN_UNIT) & {
        cr.AUDIO_HARDWARE_RECONCILE_UNIT,
        cr.OUTPUTD_UNIT,
    }
    assert cr.FANIN_UNIT not in before_of(cr.OUTPUTD_UNIT)

    serial = (
        cr._CAMILLA_REQUEUED_RECONCILE_START_SEC + cr._CAMILLA_NOTIFY_DEP_START_SEC
    )
    assert cr._CAMILLA_DEPENDENCY_CRITICAL_PATH_SEC == max(
        serial, cr._CAMILLA_NOTIFY_DEP_START_SEC
    )
    # Neither notify dependency declares a start override, so both take the
    # manager default plus their restart backoff.
    for unit in (cr.FANIN_UNIT, cr.OUTPUTD_UNIT):
        directives = dict(_unit_directives(unit))
        assert "TimeoutStartSec" not in directives, unit
        assert directives["RestartSec"] == str(int(cr._NOTIFY_DEP_RESTART_BACKOFF_SEC))


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
    # The preamble the arithmetic charges must be the bound reset_then_manage
    # actually applies, or a legitimately slow pass reads as a wedge.
    assert cr._RESET_FAILED_TIMEOUT_SEC == restart_broker._RESET_TIMEOUT_SEC
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


def test_shm_ring_is_the_only_coupling_the_cli_accepts(
    tmp_path, monkeypatch, _ring_assets_present
):
    """The one transport is the one argument; the retired token is rejected."""
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    calls: list[dict] = []

    def fake_reconcile(**kw):
        calls.append(kw)
        from jasper.fanin.coupling_reconcile import CouplingResult

        return CouplingResult(ok=True, changed=True)

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    assert cr.main([COUPLING_SHM_RING, "--no-apply"]) == 0
    assert len(calls) == 1
    with pytest.raises(SystemExit):
        cr.main(["loopback", "--no-apply"])


# --- Blocker 2: shm_ring refused while the bond reads the dac_content lane -----


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


def _coordinated(calls_ops, **kw):
    from jasper.fanin.coupling_reconcile import _restart_fanin_coordinated

    _calls, do_restart, do_stop, do_start = calls_ops
    return _restart_fanin_coordinated(
        do_restart,
        do_stop,
        do_start,
        reason="t",
        phase="test",
        **kw,
    )


def test_coordinated_ring_pauses_before_and_resumes_after():
    """The load-bearing order: camilla is STOPPED before the fan-in restart and
    STARTED after it — exactly the fan-in -> camilla order the recover script proves."""
    ops = _coord_ops()
    calls = ops[0]
    r = _coordinated(ops)
    assert calls == ["camilla_stop", "fanin", "camilla_start"]
    assert r.ok is True
    assert r.fanin_restarted is True
    assert r.camilla_stopped is True and r.camilla_started is True


def test_coordinated_stop_failure_aborts_fanin_restart_and_keeps_camilla_running():
    """Safe direction #1: if camilla can't be paused it may still be on the ring, so
    we must NOT restart fan-in (that is what SIGKILLs it). Abort, surface ok=False,
    and ensure camilla is running (start-back) — never leave it stopped-forever."""
    ops = _coord_ops(stop_ok=False)
    calls = ops[0]
    r = _coordinated(ops)
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
    r = _coordinated(ops)
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
    r = _coordinated(ops)
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


def test_outputd_actions_unset_legacy_local_content_pipe(tmp_path):
    """The outputd-action set sweeps the legacy JASPER_OUTPUTD_LOCAL_CONTENT_PIPE
    key (a removed coupling's outputd content source) so a migrating box
    converges clean."""
    sweeps = [
        a
        for a in _outputd_actions("")
        if a.action == "unset" and a.key == _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV
    ]
    assert sweeps
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
    """narrow -> wide: the flip changes the assistant width, so voice re-reads it.

    The box starts on the RETIRED token, which is the only ``before`` state that
    still resolves narrow on a declared-wide box: since #3655 an undeclared
    coupling is the ring on both sides of the language boundary, so it was
    already wide and nothing moves (the pair test below).
    """
    fanin_env = _wide_declared_env(tmp_path, "loopback")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert result.ok, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == ["voice"], (
        "a declared-wide box crossing into shm_ring changes the assistant width "
        "from S16_LE to S32_LE; voice resolves that once at start"
    )


def test_an_undeclared_box_flipping_coupling_does_not_restart_voice(
    tmp_path, _ring_assets_present
):
    """THE FLIP'S NON-CONSEQUENCE, from the other side of the pair above.

    Every box that has not pinned itself narrow resolves the ring wire resolver's
    WIDE default (`resolve_ring_wire_format`), and since #3655 an UNDECLARED
    coupling feeds the ring on the Python side too — the same answer
    `jasper-fanin` has always given it (`coupling_is_shm_ring: true`). So both
    halves of `assistant_wire_is_wide`'s conjunction were ALREADY true before the
    flip: writing the token changes nothing voice resolves, and bouncing it would
    cut the assistant for no width change. `_ring_assets_present` alone (no
    width-gate bypass) is enough here: the shipped conf.d spells S32_LE
    explicitly, matching what this undeclared box resolves, so
    `ring_edge_width_ready`/`ring_wire_caps_ready` pass on their own merits
    rather than needing to be stubbed out of the way.
    """
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert result.ok, result.detail
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert calls == [], (
        "an undeclared box is already on the wide assistant wire; the flip moves "
        "no width, so voice must not be bounced"
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
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
        restart_voice=lambda: (calls.append("voice") or (True, "")),
    )

    assert calls == []


def test_a_staging_write_never_restarts_voice(tmp_path):
    """``apply=False`` writes the env and runs NO daemon ops. Voice included."""
    fanin_env = _wide_declared_env(tmp_path, "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls = []

    _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
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
    fanin_env = _wide_declared_env(tmp_path, "")
    outputd_env = _write(tmp_path / "outputd.env", "")

    result = _reconcile(
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=lambda: (True, ""),
        restart_fanin=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
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

    Starts on the retired token for the same reason as
    `test_arming_a_declared_wide_box_restarts_voice_once`: that is the transition
    that still moves a declared-wide box's assistant width.
    """
    fanin_env = _wide_declared_env(tmp_path, "loopback")
    outputd_env = _write(tmp_path / "outputd.env", "")
    broker_calls = []

    def _manage_units(unit, *, verb, reason, no_block, timeout):
        broker_calls.append((unit, verb))
        return {"ok": True}

    monkeypatch.setattr("jasper.control.restart_broker.manage_units", _manage_units)

    result = reconcile_coupling(
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        # Everything EXCEPT restart_voice, so the only broker traffic this test
        # can observe is the join it is pinning.
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
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
    box already on the ring this time — its assistant width never moves — so the
    broker must see no voice traffic at all.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    fanin_env = _write(
        tmp_path / "fanin.env",
        f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
        f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT}\n",
    )
    outputd_env = _write(tmp_path / "outputd.env", "")
    broker_calls = []

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda unit, **kw: broker_calls.append((unit, kw.get("verb"))) or {"ok": True},
    )

    reconcile_coupling(
        reason="t",
        env_path=fanin_env,
        outputd_env_path=outputd_env,
        restart_fanin=lambda: (True, ""),
        restart_outputd=lambda: (True, ""),
        reconcile_camilla=lambda: (True, ""),
        kick_hardware_reconcile=lambda: (True, ""),
    )

    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    voice_calls = [c for c in broker_calls if c[0] == "jasper-voice.service"]
    assert voice_calls == [], (
        f"a narrow box's assistant width never moved; got {broker_calls}"
    )


def test_a_crossed_ring_pair_converges_on_the_next_pass_and_says_so(
    tmp_path, monkeypatch, caplog
):
    """RECOVERY for the marker/path pair: one pass, either direction, observable.

    The pair's two halves have two writers and cannot move in one write — the
    marker's writer (``jasper-audio-hardware-reconcile``) runs first and kicks
    this one. So a crossed pair is a normal bounded window, not a wreck, and what
    makes it bounded is that ``_outputd_actions`` derives the path from the
    marker on EVERY pass, before the transition-vs-confirm split.

    Both directions are walked, because each was a separate stall:

    * ARM (jts.local, 2026-08-21) — marker armed, path still Ring B. The
      validator refused this, so the pass that would converge it never ran.
    * DISARM — marker cleared while the coupling stayed ``shm_ring``. The
      unarmed derivation used to preserve whatever the key held, so the active
      ring's path survived every later pass and nothing ever converged.

    The heal is logged rather than silent: a box that had been refusing outputd's
    attach has just stopped, and the journal has to say when.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        DEFAULT_OUTPUTD_RING_PATH,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        OUTPUTD_RING_PATH_ENV_VAR,
    )

    def _pass(marker: str, ring_path: str) -> tuple[str, str]:
        # monkeypatch.setenv first so the reconciler's in-process env sync is
        # unwound at teardown rather than leaking into the next test.
        monkeypatch.setenv(OUTPUTD_RING_PATH_ENV_VAR, ring_path)
        fanin_env = _write(
            tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
        )
        outputd_env = _write(
            tmp_path / "outputd.env",
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n"
            f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}={marker}\n"
            f"{OUTPUTD_RING_PATH_ENV_VAR}={ring_path}\n",
        )
        _calls, ro, rf, rc = _recorder()
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _reconcile(
                fanin_env=fanin_env,
                outputd_env=outputd_env,
                restart_outputd=ro,
                restart_fanin=rf,
                reconcile_camilla=rc,
                apply=False,
            )
        return outputd_env.read_text(encoding="utf-8"), caplog.text

    armed_text, armed_log = _pass("1", DEFAULT_OUTPUTD_RING_PATH)
    assert (
        f"{OUTPUTD_RING_PATH_ENV_VAR}={DEFAULT_OUTPUTD_ACTIVE_RING_PATH}" in armed_text
    ), armed_text
    assert "result=ring_path_converged" in armed_log, armed_log
    assert DEFAULT_OUTPUTD_RING_PATH in armed_log, armed_log

    cleared_text, cleared_log = _pass("", DEFAULT_OUTPUTD_ACTIVE_RING_PATH)
    assert (
        f"{OUTPUTD_RING_PATH_ENV_VAR}={DEFAULT_OUTPUTD_RING_PATH}" in cleared_text
    ), cleared_text
    assert "result=ring_path_converged" in cleared_log, cleared_log

    # ...and an already-converged box does NOT claim a heal. Without this the
    # event would fire on every pass and mean nothing.
    _steady_text, steady_log = _pass("1", DEFAULT_OUTPUTD_ACTIVE_RING_PATH)
    assert "result=ring_path_converged" not in steady_log, steady_log
