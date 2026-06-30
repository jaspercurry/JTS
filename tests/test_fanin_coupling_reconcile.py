# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ordered arm/disarm of the fan-in -> CamillaDSP coupling.

The whole point of this reconciler is the TRANSITION ORDER (CamillaDSP's RawFile
capture crash-loops if it loads the pipe config before fan-in writes the pipe, or
keeps the pipe config after fan-in stops writing). These tests pin that order and
the fail-safe-to-loopback behavior with injected daemon-op hooks that record the
call sequence — no real systemctl / CamillaDSP needed.
"""

from __future__ import annotations

from pathlib import Path

from jasper.fanin.coupling_reconcile import (
    FANIN_ENV_PATH,
    read_persisted_coupling,
    reconcile_coupling,
)
from jasper.fanin_coupling import COUPLING_ENV_VAR


def _recorder(*, fanin_ok=True, camilla_ok=True, camilla_fail_for=None):
    """Build (calls, restart_fanin, reconcile_camilla) hooks.

    ``calls`` records the sequence; ``camilla_fail_for`` (a coupling string)
    makes only that camilla reconcile fail (so an arm can fail on RawFile yet the
    loopback recovery still succeed)."""
    calls: list[str] = []

    def restart_fanin() -> tuple[bool, str]:
        calls.append("fanin")
        return (fanin_ok, "" if fanin_ok else "restart failed")

    def reconcile_camilla(coupling: str) -> tuple[bool, str]:
        calls.append(f"camilla:{coupling}")
        ok = camilla_ok and (camilla_fail_for is None or coupling != camilla_fail_for)
        return (ok, "reconciled" if ok else "invalid config")

    return calls, restart_fanin, reconcile_camilla


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_arm_orders_fanin_before_camilla(tmp_path):
    env = tmp_path / "fanin.env"  # absent file => loopback today
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok and res.direction == "arm" and res.changed
    # fan-in writes the pipe BEFORE CamillaDSP loads the RawFile config.
    assert calls == ["fanin", "camilla:fifo"]
    assert read_persisted_coupling(env) == "fifo"


def test_coupling_reconciler_gets_env_action_from_runtime_plan():
    import jasper.fanin.coupling_reconcile as cr

    source = Path(cr.__file__).read_text(encoding="utf-8")

    assert "fanin_coupling_action" in source


def test_arm_fifo_refused_for_active_leader_keeps_loopback(tmp_path):
    env = tmp_path / "fanin.env"  # absent file => loopback today
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "fifo",
        reason="t",
        env_path=env,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )
    assert res.ok is False
    assert res.direction == "blocked"
    assert res.changed is False
    assert calls == []
    assert read_persisted_coupling(env) == "loopback"


def test_existing_fifo_recovers_to_loopback_for_active_leader(tmp_path):
    env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}=fifo\n")
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "fifo",
        reason="t",
        env_path=env,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: True,
    )
    assert res.ok is False
    assert res.direction == "blocked"
    assert res.changed is True
    assert res.recovered is True
    # Recovery uses the safe disarm order: Camilla leaves RawFile before fan-in.
    assert calls == ["camilla:loopback", "fanin"]
    assert read_persisted_coupling(env) == "loopback"


def test_disarm_orders_camilla_before_fanin(tmp_path):
    env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}=fifo\n")
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "loopback", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok and res.direction == "disarm" and res.changed
    # CamillaDSP leaves the RawFile config BEFORE fan-in stops writing the pipe.
    assert calls == ["camilla:loopback", "fanin"]
    assert read_persisted_coupling(env) == "loopback"


def test_confirm_when_already_desired_skips_fanin_restart(tmp_path):
    env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}=fifo\n")
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok and res.direction == "confirm" and not res.changed
    # No fan-in bounce on a no-op tick; camilla re-confirmed to self-heal drift.
    assert calls == ["camilla:fifo"]


def test_arm_fanin_failure_rolls_back_env_and_skips_camilla(tmp_path):
    env = tmp_path / "fanin.env"  # absent => loopback
    calls, rf, rc = _recorder(fanin_ok=False)
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok is False and res.recovered is True
    # camilla is NEVER reconciled to RawFile when the pipe has no writer.
    assert calls == ["fanin"]
    # Env rolled back: the absent file must not be left carrying =fifo.
    assert read_persisted_coupling(env) == "loopback"
    assert not env.exists()


def test_arm_camilla_failure_recovers_to_loopback(tmp_path):
    env = tmp_path / "fanin.env"
    calls, rf, rc = _recorder(camilla_fail_for="fifo")
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok is False and res.recovered is True
    # arm (fanin, camilla:fifo) then recovery (camilla:loopback, fanin).
    assert calls == ["fanin", "camilla:fifo", "camilla:loopback", "fanin"]
    assert read_persisted_coupling(env) == "loopback"


def test_disarm_camilla_failure_still_restarts_fanin(tmp_path):
    env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}=fifo\n")
    calls, rf, rc = _recorder(camilla_fail_for="loopback")
    res = reconcile_coupling(
        "loopback", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    # fan-in MUST leave the pipe even if the camilla swap failed.
    assert calls == ["camilla:loopback", "fanin"]
    assert res.ok is False and res.restarted_fanin is True


def test_unknown_value_failsafe_to_loopback(tmp_path):
    env = _write(tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}=fifo\n")
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "bogus", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    # A typo resolves to loopback (fail-safe) and disarms.
    assert res.desired == "loopback" and res.direction == "disarm"
    assert read_persisted_coupling(env) == "loopback"


def test_no_apply_writes_env_only(tmp_path):
    env = tmp_path / "fanin.env"
    calls, rf, rc = _recorder()
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, apply=False,
        restart_fanin=rf, reconcile_camilla=rc,
    )
    assert res.ok and res.changed and calls == []  # env written, no daemon ops
    assert read_persisted_coupling(env) == "fifo"


def test_arm_preserves_coexisting_buffer_key(tmp_path):
    # The adaptive-buffer sibling owns another key in the SAME file; arming the
    # coupling must not disturb it.
    env = _write(
        tmp_path / "fanin.env",
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536\n# operator note\n",
    )
    _, rf, rc = _recorder()
    reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    body = env.read_text(encoding="utf-8")
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1536" in body
    assert "# operator note" in body
    assert read_persisted_coupling(env) == "fifo"


def test_env_write_failure_aborts_before_daemon_ops(tmp_path, monkeypatch):
    env = tmp_path / "fanin.env"
    calls, rf, rc = _recorder()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("jasper.fanin.coupling_reconcile.atomic_write_text", boom)
    res = reconcile_coupling(
        "fifo", reason="t", env_path=env, restart_fanin=rf, reconcile_camilla=rc,
    )
    # A write failure must abort BEFORE bouncing a daemon into a value the file
    # does not carry.
    assert res.ok is False and res.direction == "error" and calls == []


def test_read_persisted_coupling_defaults_loopback(tmp_path):
    assert read_persisted_coupling(tmp_path / "absent.env") == "loopback"
    assert read_persisted_coupling(_write(tmp_path / "f.env", "X=1\n")) == "loopback"


def test_default_env_path_is_reconciler_owned_fanin_env():
    # Single-writer contract: same wizard-owned file the adaptive buffer + the
    # unit EnvironmentFile use.
    assert FANIN_ENV_PATH == "/var/lib/jasper/fanin.env"


def test_cli_main_hydrates_env_files_before_reconciling(monkeypatch):
    # The CLI/install shell does not pre-source the wizard env files, so main()
    # MUST hydrate them before reconciling — else the triggered camilla reconcile
    # emits with DEFAULT chunksize/target_level, silently resetting a tuned value
    # (caught on JTS 2026-06-27). Assert the hydrate happens before the reconcile.
    from jasper.fanin import coupling_reconcile as cr

    order: list[str] = []
    monkeypatch.setattr(
        "jasper.env_load.load_env_files", lambda *a, **k: order.append("hydrate")
    )

    def fake_reconcile(*a, **k):
        order.append("reconcile")
        return cr.CouplingResult(ok=True, desired="loopback", changed=False,
                                 direction="confirm")

    monkeypatch.setattr(cr, "reconcile_coupling", fake_reconcile)
    rc = cr.main(["loopback"])
    assert rc == 0
    assert order == ["hydrate", "reconcile"]
