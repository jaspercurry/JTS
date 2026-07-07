# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP transport-pipe coupling."""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.camilla_config_contract import DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE
from jasper.env_file import read_value
from jasper.fanin.coupling_reconcile import (
    FANIN_ENV_PATH,
    OUTPUTD_ENV_PATH,
    read_persisted_coupling,
    reconcile_coupling,
    validate_transport_pipe_status_window,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    OUTPUTD_RING_SLOTS_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """Keep effective-env tests independent of the developer host's /etc state."""
    jasper_env = tmp_path / "jasper.env"
    jasper_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.JASPER_ENV_PATH", str(jasper_env)
    )


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


def _outputd_value(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return read_value(text, OUTPUTD_PIPE_PATH_ENV_VAR)


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
    kwargs.setdefault("validate_transport_pipe", lambda: (True, "gate ok"))
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


def _transport_fanin_status(
    *,
    xrun_count: int = 0,
    catchup_events: int = 0,
    dropped_periods: int = 0,
    usb_frames_read: int = 0,
    usb_resampler_input_frames: int | None = None,
    usb_resampler_armed: bool = True,
    usb_resampler_locked: bool = False,
    usb_resampler_fill_frames: int = 512,
    usb_resampler_target_frames: int = 512,
    usb_resampler_unlock_count: int = 0,
    usb_resampler_overrun_frames: int = 0,
) -> dict[str, object]:
    usb_input: dict[str, object] = {
        "label": "usbsink",
        "frames_read": usb_frames_read,
        "xrun_count": xrun_count,
        "catchup_events": catchup_events,
    }
    if usb_resampler_input_frames is not None:
        usb_input["resampler"] = {
            "armed": usb_resampler_armed,
            "locked": usb_resampler_locked,
            "input_frames": usb_resampler_input_frames,
            "fill_frames": usb_resampler_fill_frames,
            "target_fill_frames": usb_resampler_target_frames,
            "unlock_count": usb_resampler_unlock_count,
            "overrun_frames": usb_resampler_overrun_frames,
        }
    return {
        "output": {
            "transport": "transport_pipe",
            "period_frames": 256,
            "xrun_count": 0,
            "pipe": {
                "actual_pipe_bytes": 8192,
                "dropped_periods": dropped_periods,
            },
        },
        "inputs": [usb_input],
    }


def _transport_outputd_status(
    *,
    available_bytes: int = 1024,
    empty_periods: int = 0,
    partial_periods: int = 0,
    dac_xruns: int = 0,
    local_pipe_format: str = "S32_LE",
) -> dict[str, object]:
    return {
        "content": {
            "source": "local_pipe",
            "channels": 2,
            "period_frames": 256,
            "empty_periods": empty_periods,
            "partial_periods": partial_periods,
            "xrun_count": 0,
            "local_pipe": {
                "open": True,
                "format": local_pipe_format,
                "actual_pipe_bytes": 8192,
                "available_bytes": available_bytes,
            },
        },
        "dac": {
            "period_frames": 256,
            "xrun_count": dac_xruns,
        },
    }


def test_arm_orders_outputd_then_fanin_then_camilla_and_writes_both_envs(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.direction == "arm" and res.changed
    assert calls == ["outputd", "fanin", "camilla:transport_pipe"]
    assert read_persisted_coupling(fanin_env) == COUPLING_TRANSPORT_PIPE
    assert _outputd_value(outputd_env) == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE


def test_coupling_reconciler_gets_env_action_from_runtime_plan():
    import jasper.fanin.coupling_reconcile as cr

    source = Path(cr.__file__).read_text(encoding="utf-8")

    assert "fanin_coupling_action" in source


def test_transport_pipe_refused_for_active_leader_keeps_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )

    assert res.ok is False
    assert res.direction == "blocked"
    assert res.changed is False
    assert calls == []
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_existing_transport_pipe_recovers_to_loopback_for_active_leader(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )

    assert res.ok is False
    assert res.direction == "blocked"
    assert res.changed is True
    assert res.recovered is True
    assert calls == ["camilla:loopback", "fanin", "outputd"]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_disarm_orders_camilla_before_fanin_before_outputd(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        "loopback",
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.direction == "disarm" and res.changed
    assert calls == ["camilla:loopback", "fanin", "outputd"]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_confirm_when_already_desired_skips_daemon_restarts(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.direction == "confirm" and not res.changed
    assert calls == ["camilla:transport_pipe"]


def test_transport_confirm_repairs_missing_outputd_pipe_env(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.direction == "arm" and res.changed
    assert calls == ["outputd", "fanin", "camilla:transport_pipe"]
    assert _outputd_value(outputd_env) == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE


def test_arm_outputd_failure_recovers_to_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder(outputd_ok=False)

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok is False
    assert calls == ["outputd", "camilla:loopback", "fanin", "outputd"]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_arm_fanin_failure_recovers_to_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder(fanin_ok=False)

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok is False
    assert calls == ["outputd", "fanin", "camilla:loopback", "fanin", "outputd"]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_arm_camilla_failure_recovers_to_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder(camilla_fail_for=COUPLING_TRANSPORT_PIPE)

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok is False
    assert calls == [
        "outputd",
        "fanin",
        "camilla:transport_pipe",
        "camilla:loopback",
        "fanin",
        "outputd",
    ]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_arm_transport_pipe_gate_failure_recovers_to_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        validate_transport_pipe=lambda: (False, "fan-in catchup climbed"),
    )

    assert res.ok is False
    assert res.direction == "arm"
    assert res.recovered is True
    assert res.validated_transport_pipe is False
    assert "fan-in catchup climbed" in res.detail
    assert calls == [
        "outputd",
        "fanin",
        "camilla:transport_pipe",
        "camilla:loopback",
        "fanin",
        "outputd",
    ]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_transport_pipe_gate_exception_recovers_to_loopback(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    def gate_raises():
        raise RuntimeError("socket parser bug")

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        validate_transport_pipe=gate_raises,
    )

    assert res.ok is False
    assert res.recovered is True
    assert "activation gate raised: socket parser bug" in res.detail
    assert calls == [
        "outputd",
        "fanin",
        "camilla:transport_pipe",
        "camilla:loopback",
        "fanin",
        "outputd",
    ]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_confirm_transport_pipe_gate_failure_recovers_to_loopback(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
    )
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        validate_transport_pipe=lambda: (False, "outputd pipe queued latency"),
    )

    assert res.ok is False
    assert res.direction == "confirm"
    assert res.changed is True
    assert res.recovered is True
    assert calls == [
        "camilla:transport_pipe",
        "camilla:loopback",
        "fanin",
        "outputd",
    ]
    assert read_persisted_coupling(fanin_env) == "loopback"
    assert _outputd_value(outputd_env) is None


def test_disarm_camilla_failure_still_restarts_fanin_and_outputd(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
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
    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
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
    assert _outputd_value(outputd_env) is None


def test_no_apply_writes_both_envs_only(tmp_path):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        apply=False,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    assert res.ok and res.changed and calls == []
    assert res.direction == "arm"  # transport_pipe is an arm
    assert read_persisted_coupling(fanin_env) == COUPLING_TRANSPORT_PIPE
    assert _outputd_value(outputd_env) == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE


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
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_TRANSPORT_PIPE}\n"
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}={DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE}\n",
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


def test_arm_preserves_coexisting_keys_and_custom_outputd_pipe(tmp_path):
    fanin_env = _write(
        tmp_path / "fanin.env",
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536\n# operator note\n",
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        "JASPER_CAMILLA_CHUNKSIZE=256\n"
        f"{OUTPUTD_PIPE_PATH_ENV_VAR}=/run/custom/content.pipe\n",
    )
    _, ro, rf, rc = _recorder()

    _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
    )

    fanin_body = fanin_env.read_text(encoding="utf-8")
    outputd_body = outputd_env.read_text(encoding="utf-8")
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536" in fanin_body
    assert "# operator note" in fanin_body
    assert "JASPER_CAMILLA_CHUNKSIZE=256" in outputd_body
    assert _outputd_value(outputd_env) == "/run/custom/content.pipe"


def test_env_write_failure_aborts_before_daemon_ops(tmp_path, monkeypatch):
    fanin_env = tmp_path / "fanin.env"
    outputd_env = tmp_path / "outputd.env"
    calls, ro, rf, rc = _recorder()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("jasper.fanin.coupling_reconcile.atomic_write_text", boom)
    res = _reconcile(
        COUPLING_TRANSPORT_PIPE,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
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


def test_cli_main_reports_transport_gate(monkeypatch, capsys):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)

    def fake_reconcile(*a, **k):
        return cr.CouplingResult(
            ok=True,
            desired=COUPLING_TRANSPORT_PIPE,
            changed=True,
            direction="arm",
            validated_transport_pipe=True,
        )

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main([COUPLING_TRANSPORT_PIPE])

    assert rc == 0
    assert "transport_gate=True" in capsys.readouterr().out


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

    fanin_env = _write(tmp_path / "fanin.env", "JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
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


def test_cli_auto_dispatches_to_reconcile_auto(monkeypatch, capsys):
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    seen = {}

    def fake_auto(*a, **k):
        seen.update(k)
        return cr.AutoResult(
            ok=True, owned=True, coupling="shm_ring", gadget_present=True,
            usb_combo_changed=True, reason="all ring gates passed",
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


def test_transport_pipe_status_gate_accepts_stable_window():
    fanin_samples = [
        _transport_fanin_status(),
        _transport_fanin_status(xrun_count=1, catchup_events=2),
    ]
    outputd_samples = [
        _transport_outputd_status(),
        _transport_outputd_status(empty_periods=2, partial_periods=1),
    ]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is True
    assert "activation gate ok" in detail


def test_transport_pipe_status_gate_rejects_fanin_counter_runaway():
    fanin_samples = [
        _transport_fanin_status(),
        _transport_fanin_status(xrun_count=20, catchup_events=40),
    ]
    outputd_samples = [
        _transport_outputd_status(),
        _transport_outputd_status(),
    ]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "fan-in input usbsink xrun_count delta=20" in detail
    assert "fan-in input usbsink catchup_events delta=40" in detail


def test_transport_pipe_status_gate_rejects_outputd_queued_latency():
    fanin_samples = [
        _transport_fanin_status(),
        _transport_fanin_status(),
    ]
    outputd_samples = [
        _transport_outputd_status(),
        _transport_outputd_status(available_bytes=32_768),
    ]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "hidden queued latency" in detail


def test_transport_pipe_status_gate_rejects_outputd_local_pipe_format_mismatch():
    fanin_samples = [
        _transport_fanin_status(),
        _transport_fanin_status(),
    ]
    outputd_samples = [
        _transport_outputd_status(local_pipe_format="S16_LE"),
        _transport_outputd_status(local_pipe_format="S16_LE"),
    ]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "local content pipe format mismatch" in detail


def test_transport_pipe_status_gate_accepts_active_usb_locked_resampler():
    fanin_samples = [
        _transport_fanin_status(
            usb_frames_read=1_000,
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=True,
            usb_resampler_fill_frames=512,
        ),
        _transport_fanin_status(
            usb_frames_read=8_680,
            usb_resampler_input_frames=8_680,
            usb_resampler_locked=True,
            usb_resampler_fill_frames=540,
        ),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is True
    assert "activation gate ok" in detail


def test_transport_pipe_status_gate_rejects_active_usb_unlocked_resampler():
    fanin_samples = [
        _transport_fanin_status(
            usb_frames_read=1_000,
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=False,
        ),
        _transport_fanin_status(
            usb_frames_read=8_680,
            usb_resampler_input_frames=8_680,
            usb_resampler_locked=False,
        ),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "fan-in input usbsink resampler not locked" in detail


def test_transport_pipe_status_gate_rejects_active_usb_missing_resampler():
    fanin_samples = [
        _transport_fanin_status(usb_frames_read=1_000),
        _transport_fanin_status(usb_frames_read=8_680),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "fan-in input usbsink resampler missing" in detail


def test_transport_pipe_status_gate_rejects_active_usb_resampler_unlock():
    fanin_samples = [
        _transport_fanin_status(
            usb_frames_read=1_000,
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=True,
            usb_resampler_unlock_count=0,
        ),
        _transport_fanin_status(
            usb_frames_read=8_680,
            usb_resampler_input_frames=8_680,
            usb_resampler_locked=True,
            usb_resampler_unlock_count=1,
        ),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "fan-in input usbsink resampler unlock_count delta=1" in detail


def test_transport_pipe_status_gate_rejects_active_usb_resampler_fill_drift():
    fanin_samples = [
        _transport_fanin_status(
            usb_frames_read=1_000,
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=True,
            usb_resampler_fill_frames=512,
            usb_resampler_target_frames=512,
        ),
        _transport_fanin_status(
            usb_frames_read=8_680,
            usb_resampler_input_frames=8_680,
            usb_resampler_locked=True,
            usb_resampler_fill_frames=2_000,
            usb_resampler_target_frames=512,
        ),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is False
    assert "fan-in input usbsink resampler fill_frames=2000" in detail


def test_transport_pipe_status_gate_allows_idle_usb_unlocked_resampler():
    fanin_samples = [
        _transport_fanin_status(
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=False,
        ),
        _transport_fanin_status(
            usb_resampler_input_frames=1_000,
            usb_resampler_locked=False,
        ),
    ]
    outputd_samples = [_transport_outputd_status(), _transport_outputd_status()]

    ok, detail = validate_transport_pipe_status_window(
        warmup_seconds=0,
        window_seconds=0,
        read_fanin_status=lambda: (fanin_samples.pop(0), ""),
        read_outputd_status=lambda: (outputd_samples.pop(0), ""),
    )

    assert ok is True
    assert "activation gate ok" in detail


# --- shm_ring coupling (Ring A + Ring B, P2) ---------------------------------


@pytest.fixture
def _ring_assets_present(monkeypatch):
    """Force the shm_ring activation gates to pass (assets + all geometry axes).

    Every PREFLIGHT must pass for an arm to proceed: assets present, the conf.d
    ring period matching outputd's resolved period, AND the Ring-A slot count
    matching. Tests about the ARM SPINE (order, camilla-failure rollback, disarm)
    stub all of them so they exercise the daemon path; the geometry-mismatch and
    slot-mismatch behaviours have their own dedicated tests below (which do NOT use
    this fixture). The stale-ring-file guard is also stubbed to a no-op so the
    spine tests don't touch /dev/shm.
    """
    import jasper.ring_assets as ra
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr(
        ra,
        "ring_asset_presence",
        lambda **kw: ra.RingAssetPresence(True, True, True),
    )
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
    # The stale-file guard reads /dev/shm; stub it to a no-op for spine tests.
    monkeypatch.setattr(
        cr, "_delete_stale_ring_files", lambda reason, fanin_text="": None
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
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(conf))

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
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(conf))

    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(
        tmp_path / "outputd.env", "JASPER_OUTPUTD_PERIOD_FRAMES=128\n"
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

    assert result.ok is True
    assert result.direction == "arm"
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


# --- defect A: Ring-A slot-count coherence + stale-file guard + migration -----


def _ring_conf(tmp_path, *, capture_n_slots: int = 2, period_frames: int = 128):
    """Write a ring conf.d with a configurable jts_ring_capture n_slots.

    period_frames stays 128 (the Apple-dongle floor) so the SEPARATE period gate
    passes when outputd's env carries JASPER_OUTPUTD_PERIOD_FRAMES=128; these tests
    isolate the slot axis.
    """
    conf = tmp_path / "60-jts-ring.conf"
    conf.write_text(
        f"pcm.jts_ring_capture {{\n    period_frames {period_frames}\n"
        f"    n_slots {capture_n_slots}\n}}\n"
        f"pcm.jts_ring_playback {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n",
        encoding="utf-8",
    )
    return conf


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


def test_arm_shm_ring_overrides_stale_base_ring_slots_then_arms(
    tmp_path, monkeypatch
):
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
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
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
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots, period_frames=128):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
        struct.pack_into("<I", hdr, 20, period_frames)
        struct.pack_into("<I", hdr, 24, n_slots)
        path.write_bytes(bytes(hdr) + b"\x00" * 512)

    _write_ring(program, 8)  # old Ring A default from the already-armed box
    _write_ring(content, 2)  # Ring B was already minimal

    fanin_env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n")
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
        assert not program.exists(), "stale Ring A must be deleted before Camilla reload"
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
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    import struct

    def _write_ring(path, n_slots):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
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
        ra, "RING_CONF_D", str(_ring_conf(tmp_path, capture_n_slots=2, period_frames=128))
    )
    program = tmp_path / "program.ring"
    content = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(program))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(content))

    def _write_ring(path, n_slots, period_frames):
        hdr = bytearray(128)
        struct.pack_into("<I", hdr, 0, 0x4A52_494E)  # magic JRIN
        struct.pack_into("<I", hdr, 4, 1)  # version
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
    assert not program.exists(), "stale-period Ring A must be deleted on CONFIRM self-heal"
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


def test_arm_shm_ring_topology_unreadable_is_failsafe_not_blocking(
    tmp_path, monkeypatch, _ring_assets_present
):
    # Fail-safe: an UNREADABLE topology (transient) must NOT refuse a legitimate
    # arm — outputd's own guard is the backstop. The gate returns eligible.
    from jasper.output_topology import OutputTopologyError

    def _boom(*a, **k):
        raise OutputTopologyError("corrupt topology")

    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", _boom
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

    assert result.ok is True
    assert result.direction == "arm"
    assert calls == ["outputd", "fanin", "camilla:shm_ring"]


# --- DEFECT 2: ring_topology_ready end-to-end over REAL on-disk topologies ----
# The tests above either exercise the topology_supports_shm_ring predicate in
# isolation (tests/test_runtime_contract_ring.py) or MOCK the predicate at the
# reconciler seam. Neither proves the actual arming gate the defect names
# (arm_ring_topology_ineligible) resolves a REAL shipped-default topology loaded
# from disk via load_output_topology_strict() to "eligible" — nor that a real
# stale-subwoofer topology honestly refuses through that same gate. These close
# that gap: a plain-stereo single-sink box (jts.local's true hardware) must NOT
# be refused, and the refusal that DID fire on jts.local was the honest verdict
# on its saved stale-subwoofer artifact, remediated by the reset tool.


def test_ring_topology_ready_eligible_for_real_shipped_default_topology(
    tmp_path, monkeypatch,
):
    # The positive end-to-end path: a genuine on-disk shipped-default Apple-dongle
    # topology (empty speaker_groups, outputs state="unused" — what
    # new_topology_draft writes on a fresh box) resolves through the reconciler's
    # ring_topology_ready() gate to a TRUE, non-fail-safe verdict. jts.local must
    # arm shm_ring once its stale artifacts are cleared.
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
                    "outputs": [
                        {"index": 0, "human_label": "Left", "terminal_label": "1",
                         "state": "unused"},
                        {"index": 1, "human_label": "Right", "terminal_label": "2",
                         "state": "unused"},
                    ],
                },
                "speaker_groups": [],
                "routing": {},
            }
        )
    )

    ok, detail = ring_topology_ready()

    assert ok is True
    # A genuine eligible verdict, NOT the "topology unreadable ... deferring"
    # fail-safe (which would also return True but for the wrong reason).
    assert "ring-eligible" in detail
    assert "unreadable" not in detail


def test_ring_topology_ready_refuses_real_stale_subwoofer_with_reset_hint(
    tmp_path, monkeypatch,
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
                        "channels": [
                            {"role": "subwoofer", "physical_output_index": 0}
                        ],
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
    outputd_env = _write(
        tmp_path / "outputd.env", "JASPER_OUTPUTD_SINK=dual_apple\n"
    )
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
    assert read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR) == "shm_ring"

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


def test_shm_ring_cli_choices_accept_ring(tmp_path, monkeypatch, _ring_assets_present):
    # The CLI must accept shm_ring as a valid coupling argument.
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    captured = {}

    def fake_reconcile(coupling, **kw):
        captured["coupling"] = coupling
        from jasper.fanin.coupling_reconcile import CouplingResult

        return CouplingResult(
            ok=True, desired=coupling, changed=True, direction="arm"
        )

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main(["shm_ring", "--no-apply"])
    assert rc == 0
    assert captured["coupling"] == "shm_ring"


# --- Blocker 2: shm_ring refused while the box is bonded/grouping-enabled ------


def test_arm_shm_ring_refused_for_active_leader_keeps_loopback(tmp_path):
    # BLOCKER 2: arming shm_ring on a bonded box must be REFUSED before any daemon
    # op (the ring is solo-stereo-only until ring v2 / P8). Never touch the rings;
    # keep loopback. Reports the real desired coupling, not a hardcoded one.
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


def test_arm_shm_ring_refused_for_active_follower(tmp_path, monkeypatch):
    # The block covers a FOLLOWER too (not just leader) — grouping-enabled is the
    # gate. Drive the route mode through the real grouping-config reader.
    import jasper.audio_runtime_plan as arp

    class _Cfg:
        enabled = True
        error = None
        role = "follower"

    monkeypatch.setattr(arp, "route_mode_from_grouping_config", lambda cfg: "active_follower")
    monkeypatch.setattr(
        "jasper.multiroom.config.load_config", lambda *a, **k: _Cfg(), raising=False
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
        # active_leader_check omitted -> falls through to the grouping-config reader.
    )

    assert result.ok is False
    assert result.direction == "blocked"
    assert result.desired == COUPLING_SHM_RING
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


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
    assert read_value(outputd_env.read_text(), OUTPUTD_CONTENT_BRIDGE_ENV_VAR) == "shm_ring"

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
