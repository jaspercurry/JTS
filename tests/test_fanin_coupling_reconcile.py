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
    assert read_persisted_coupling(fanin_env) == COUPLING_TRANSPORT_PIPE
    assert _outputd_value(outputd_env) == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE


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
    """Force the shm_ring activation gates (asset presence + slot geometry) to pass.

    Both PREFLIGHTs must pass for an arm to proceed: assets present AND the conf.d
    ring period matching outputd's resolved period. Tests about the ARM SPINE (order,
    camilla-failure rollback, disarm) stub both so they exercise the daemon path;
    the geometry-mismatch behaviour has its own dedicated tests below.
    """
    import jasper.ring_assets as ra

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
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 8\n}\n"
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
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 8\n}\n"
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
