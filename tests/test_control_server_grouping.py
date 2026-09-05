# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route tests for ``jasper.control.handlers.grouping``.

/grouping and /grouping/set — role/roster validation, the settable
field parsers, and the reconciler kick and trailing-apply scheduling.
"""

from __future__ import annotations

import json
import subprocess

import pytest


from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _grouping_test_setup,
    _isolate_household_secret,
    _post,
    server_with_coordinator,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
)


# ---------- POST /grouping/set (the bond-forming control endpoint) ----------


_GROUPING_KICK = [
    "/usr/local/sbin/jasper-grouping-reconcile-kick",
]


def test_grouping_set_leader_writes_env_and_kicks_reconciler(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "leader", "channel": "left",
        "bond_id": "living-room",
    })

    assert status == 200
    assert body["ok"] is True
    assert body["role"] == "leader" and body["channel"] == "left"
    text = env.read_text()
    assert "JASPER_GROUPING=on" in text
    assert "JASPER_GROUPING_ROLE=leader" in text
    assert "JASPER_GROUPING_CHANNEL=left" in text
    assert "JASPER_GROUPING_BOND_ID=living-room" in text
    assert _GROUPING_KICK in popens


def test_grouping_set_enable_rejects_active_speaker_setup_block(
    monkeypatch, tmp_path, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "baseline_summed_validation_missing",
            "detail": "validate the combined crossover before saving the active profile",
        },
    )

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "leader", "channel": "left",
        "bond_id": "living-room",
    })

    assert status == 409
    assert "validate the combined crossover" in body["error"]
    assert body["active_speaker_setup"]["grouping_allowed"] is False
    assert not env.exists()
    assert popens == []


def test_grouping_set_disabled_writes_off_and_kicks(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {"enabled": False})

    assert status == 200
    assert body["enabled"] is False
    assert "JASPER_GROUPING=off" in env.read_text()
    assert _GROUPING_KICK in popens


def test_grouping_set_disabled_ignores_active_speaker_setup_block(
    monkeypatch, tmp_path, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "baseline_summed_validation_missing",
            "detail": "validate the combined crossover before saving the active profile",
        },
    )

    status, body = _post(f"{base}/grouping/set", {"enabled": False})

    assert status == 200
    assert body["enabled"] is False
    assert "JASPER_GROUPING=off" in env.read_text()
    assert _GROUPING_KICK in popens


def test_grouping_set_delay_burst_coalesces_kicks_and_applies_last_env(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """A delay/crossover sweep can POST /grouping/set every few seconds.

    The first write still kicks promptly, but the rest of the cooldown window
    collapses to one trailing reconciler run. That trailing run re-reads the
    current grouping.env, so the final swept value is never lost.
    """
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    env = tmp_path / "grouping.env"
    now = [1000.0]
    timers = []
    applied: list[tuple[str, str]] = []

    class FakeHandle:
        daemon = False

        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert not self.cancelled
            self.callback()

    def launch(reason: str) -> None:
        applied.append((reason, env.read_text()))

    def schedule_trailing(delay, run_trailing, _mark_applied):
        handle = FakeHandle(delay, run_trailing)
        timers.append(handle)
        return handle

    monkeypatch.setattr(srv_mod, "GROUPING_ENV_FILE", str(env))
    monkeypatch.setattr(
        srv_mod,
        "_grouping_reconciler_kick_coalescer",
        srv_mod._GroupingReconcilerKickCoalescer(
            cooldown_s=60.0,
            launch=launch,
            clock=lambda: now[0],
            trailing_scheduler=schedule_trailing,
            cancel_external_trailing=lambda: None,
        ),
    )

    body = {
        "enabled": True,
        "role": "follower",
        "channel": "right",
        "bond_id": "living-room",
        "leader_addr": "jts.local",
    }
    offsets_and_delays = [
        (0.0, 0.5),
        (7.0, 1.0),
        (16.0, 1.5),
        (27.0, 2.0),
        (35.0, 2.5),
        (44.0, 3.0),
    ]

    for offset, left_delay_ms in offsets_and_delays:
        now[0] = 1000.0 + offset
        status, body_resp = _post(
            f"{base}/grouping/set",
            {**body, "left_delay_ms": left_delay_ms},
        )
        assert status == 200, body_resp

    assert [reason for reason, _text in applied] == ["leading"]
    assert "JASPER_GROUPING_LEFT_DELAY_MS=0.500" in applied[0][1]
    assert len(timers) == 1
    assert timers[0].delay == pytest.approx(53.0)
    assert "JASPER_GROUPING_LEFT_DELAY_MS=3.000" in env.read_text()

    now[0] = 1060.0
    timers[0].fire()

    assert [reason for reason, _text in applied] == ["leading", "trailing"]
    assert "JASPER_GROUPING_LEFT_DELAY_MS=3.000" in applied[-1][1]


def test_grouping_set_trim_only_live_applies_without_reconciler(
    monkeypatch, tmp_path, server_with_coordinator,
):
    import jasper.control.server as srv_mod
    from jasper.multiroom.runtime_balance import LiveTrimApplyResult

    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)
    env.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=right\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_LEADER_ADDR=jts.local\n"
        "JASPER_GROUPING_TRIM_DB=0.0\n"
    )
    calls = []

    async def fake_live_apply(trim_db, *, cfg):
        calls.append((trim_db, cfg.role, cfg.channel))
        return LiveTrimApplyResult(True, "outputd", trim_db)

    monkeypatch.setattr(srv_mod, "apply_live_grouping_trim", fake_live_apply)

    status, body = _post(
        f"{base}/grouping/set",
        {
            "enabled": True,
            "role": "follower",
            "channel": "right",
            "bond_id": "living-room",
            "leader_addr": "jts.local",
            "trim_db": -3.0,
        },
    )

    assert status == 200
    assert body["live_apply"] == {
        "applied": True,
        "mode": "outputd",
        "trim_db": -3.0,
    }
    assert body["reconciler_kicked"] is False
    assert calls == [(-3.0, "follower", "right")]
    assert popens == []
    assert "JASPER_GROUPING_TRIM_DB=-3.0" in env.read_text()


def test_grouping_set_trim_only_falls_back_to_reconciler_on_live_apply_failure(
    monkeypatch, tmp_path, server_with_coordinator,
):
    import jasper.control.server as srv_mod
    from jasper.multiroom.runtime_balance import LiveTrimApplyResult

    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)
    env.write_text(
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=right\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_LEADER_ADDR=jts.local\n"
        "JASPER_GROUPING_TRIM_DB=0.0\n"
    )

    async def fake_live_apply(trim_db, *, cfg):
        return LiveTrimApplyResult(False, "outputd", trim_db, "socket down")

    monkeypatch.setattr(srv_mod, "apply_live_grouping_trim", fake_live_apply)

    status, body = _post(
        f"{base}/grouping/set",
        {
            "enabled": True,
            "role": "follower",
            "channel": "right",
            "bond_id": "living-room",
            "leader_addr": "jts.local",
            "trim_db": -3.0,
        },
    )

    assert status == 200
    assert body["live_apply"] == {
        "applied": False,
        "mode": "outputd",
        "trim_db": -3.0,
        "detail": "socket down",
    }
    assert body["reconciler_kicked"] is True
    assert _GROUPING_KICK in popens


def test_grouping_trailing_scheduler_arms_durable_service(monkeypatch, tmp_path):
    import jasper.control.server as srv_mod

    run_calls = []
    timers = []
    marks = []
    launches = []
    delay_file = tmp_path / "grouping-reconcile-trailing-delay"

    class FakeTimer:
        daemon = False

        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert self.started
            assert not self.cancelled
            self.callback()

    def fake_run(cmd, **kwargs):
        run_calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        srv_mod,
        "_GROUPING_RECONCILE_TRAILING_DELAY_FILE",
        str(delay_file),
    )

    srv_mod._schedule_grouping_reconciler_trailing_kick(
        53.0,
        lambda: launches.append("trailing"),
        lambda: marks.append("applied"),
        timer_factory=FakeTimer,
    )

    assert run_calls == [(
        [
            "systemctl",
            "restart",
            "--no-block",
            "jasper-grouping-reconcile-trailing.service",
        ],
        {
            "check": True,
            "stdout": srv_mod.subprocess.DEVNULL,
            "stderr": srv_mod.subprocess.DEVNULL,
        },
    )]
    assert len(timers) == 1
    assert timers[0].delay == pytest.approx(53.0)
    assert delay_file.read_text() == "53\n"

    timers[0].fire()

    assert marks == ["applied"]
    assert launches == []


def test_grouping_trailing_scheduler_falls_back_to_process_timer(
    monkeypatch, tmp_path,
):
    import jasper.control.server as srv_mod

    timers = []
    marks = []
    launches = []
    delay_file = tmp_path / "grouping-reconcile-trailing-delay"

    class FakeTimer:
        daemon = False

        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert self.started
            assert not self.cancelled
            self.callback()

    def fake_run(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        srv_mod,
        "_GROUPING_RECONCILE_TRAILING_DELAY_FILE",
        str(delay_file),
    )

    srv_mod._schedule_grouping_reconciler_trailing_kick(
        12.0,
        lambda: launches.append("trailing"),
        lambda: marks.append("applied"),
        timer_factory=FakeTimer,
    )

    assert len(timers) == 1
    assert timers[0].delay == pytest.approx(12.0)
    assert delay_file.read_text() == "12\n"

    timers[0].fire()

    assert launches == ["trailing"]
    assert marks == []


def test_grouping_trailing_delay_file_is_clamped_and_rounded(
    monkeypatch, tmp_path,
):
    import jasper.control.server as srv_mod

    delay_file = tmp_path / "grouping-reconcile-trailing-delay"
    monkeypatch.setattr(
        srv_mod,
        "_GROUPING_RECONCILE_TRAILING_DELAY_FILE",
        str(delay_file),
    )

    srv_mod._write_grouping_reconciler_trailing_delay(12.2)
    assert delay_file.read_text() == "13\n"

    srv_mod._write_grouping_reconciler_trailing_delay(999.0)
    assert delay_file.read_text() == "60\n"


def test_grouping_set_rejects_invalid_role_without_writing(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "boss", "channel": "left", "bond_id": "x",
    })

    assert status == 400
    assert "ROLE" in body["error"]
    assert not env.exists()   # nothing persisted on a rejected request
    assert _GROUPING_KICK not in popens   # grouping reconciler not kicked


def test_grouping_set_follower_requires_leader_addr(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "follower", "channel": "right", "bond_id": "x",
    })

    assert status == 400
    assert "LEADER_ADDR" in body["error"]
    assert not env.exists()
    assert _GROUPING_KICK not in popens


def test_grouping_set_disabled_path_validates_roster_no_persist(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """A `disabled` /grouping/set still VALIDATES a roster it carries. The
    disabled path skips validate_grouping, but the persisted roster IS the unbond
    disable list, so a member with a foreign/public addr must be rejected 400 and
    nothing persisted — closing the disabled-path roster-injection hole (an
    injected foreign addr would otherwise become an unbond disable target)."""
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": False,
        "roster": [{"addr": "8.8.8.8", "name": "x", "channel": "right"}],
    })

    assert status == 400
    assert "ROSTER" in body["error"]
    assert not env.exists()           # nothing persisted on a rejected request
    assert _GROUPING_KICK not in popens


# ---------- GET /grouping (the dissolve-flow read endpoint) ----------


def test_grouping_get_returns_grouping_block(
    monkeypatch, server_with_coordinator,
):
    """GET /grouping returns read_grouping_state() under a `grouping`
    key — the block the dissolve flow reads to discover bond
    membership (role, bond_id, leader_addr)."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    snapshot = {
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "living-room",
        "leader_addr": "",
        "buffer_ms": 1000,
        "codec": "flac",
        "error": None,
    }
    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: snapshot)
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda: {"active": False, "grouping_allowed": True},
    )

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] == snapshot
    assert body["readiness"] == {"allowed": True, "detail": "ready"}
    # Cross-boundary contract: the /rooms /unbond CONSUMER must extract the
    # snapshot from the PRODUCER's actual body via the shared parser. Running
    # the real emitted body through parse_grouping_response here is what would
    # have caught the C4 drift (the two daemons no longer test only their own
    # half of the contract in isolation).
    from jasper.multiroom.state import parse_grouping_response
    assert parse_grouping_response(body) == snapshot


def test_grouping_get_projects_readiness_under_peer_response_budget(
    monkeypatch, server_with_coordinator,
):
    """The small grouping endpoint must not inherit setup diagnostics.

    This binds the real producer body to the /rooms peer-reader cap. The
    original pairing regression fetched /state and crossed that cap when an
    unrelated diagnostic block grew; a large source snapshot here proves the
    public readiness projection remains small with ample headroom.
    """
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod
    from jasper.web.rooms_setup import PEER_RESPONSE_MAX_BYTES

    monkeypatch.setattr(
        srv_mod,
        "read_grouping_state",
        lambda: {
            "enabled": False,
            "role": "",
            "channel": "stereo",
            "bond_id": "",
            "leader_addr": "",
            "buffer_ms": 400,
            "codec": "flac",
            "roster": [],
            "error": None,
        },
    )
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda: {
            "active": True,
            "grouping_allowed": False,
            "detail": "Finish active-speaker commissioning.",
            "large_unrelated_diagnostics": "x" * PEER_RESPONSE_MAX_BYTES,
        },
    )

    status, body = _get(f"{base}/grouping")
    encoded = json.dumps(body).encode("utf-8")

    assert status == 200
    assert body["readiness"] == {
        "allowed": False,
        "detail": "Finish active-speaker commissioning.",
    }
    assert len(encoded) < PEER_RESPONSE_MAX_BYTES // 8


def test_grouping_get_requires_no_csrf(monkeypatch, server_with_coordinator):
    """A plain GET (no Origin / CSRF token) succeeds — /grouping is an
    unauthenticated read on this no-auth LAN surface, like /state and
    /healthz."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: {"enabled": False})
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda: {"active": False, "grouping_allowed": True},
    )

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] == {"enabled": False}
    assert body["readiness"]["allowed"] is True


def test_grouping_get_fails_soft_on_read_error(
    monkeypatch, server_with_coordinator,
):
    """If read_grouping_state raises, /grouping still returns 200 with a
    null grouping payload rather than 500 — mirrors /state's fail-soft
    grouping section."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def boom():
        raise RuntimeError("grouping read exploded")

    monkeypatch.setattr(srv_mod, "read_grouping_state", boom)
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda: {"active": False, "grouping_allowed": True},
    )

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] is None
    assert body["readiness"] == {"allowed": True, "detail": "ready"}


def test_grouping_get_surfaces_target_side_active_speaker_block(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """The lightweight preflight and final write guard return one verdict."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    env, popens = _grouping_test_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: {"enabled": False})
    monkeypatch.setattr(
        srv_mod,
        "read_active_speaker_setup_status",
        lambda: {
            "active": True,
            "grouping_allowed": False,
            "detail": "Finish active-speaker commissioning.",
        },
    )

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["readiness"] == {
        "allowed": False,
        "detail": "Finish active-speaker commissioning.",
    }

    write_status, write_body = _post(f"{base}/grouping/set", {
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "living-room",
    })
    assert write_status == 409
    assert write_body["error"] == body["readiness"]["detail"]
    assert not env.exists()
    assert popens == []


def test_grouping_get_fails_readiness_closed_without_hiding_grouping(
    monkeypatch, server_with_coordinator,
):
    """A broken readiness derivation stays explicit null and grouping survives."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    snapshot = {"enabled": False}
    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: snapshot)

    def boom():
        raise RuntimeError("active setup read exploded")

    monkeypatch.setattr(srv_mod, "read_active_speaker_setup_status", boom)

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body == {"grouping": snapshot, "readiness": None}


@pytest.mark.parametrize(
    "omitted",
    (
        "trim_db",
        "client_latency_ms",
        "left_delay_ms",
        "right_delay_ms",
    ),
)
def test_grouping_optional_field_parser_preserves_omission(omitted: str) -> None:
    import jasper.control.server as srv_mod

    body = {
        "trim_db": -2.5,
        "client_latency_ms": 11,
        "left_delay_ms": 1.25,
        "right_delay_ms": 0.5,
    }
    body.pop(omitted)

    parsed, error = srv_mod._parse_grouping_optional_fields(body)

    assert error is None
    assert parsed is not None
    assert getattr(parsed, omitted) is None
    for key, value in body.items():
        assert getattr(parsed, key) == value


def test_grouping_optional_numeric_parser_preserves_python_coercions() -> None:
    import jasper.control.server as srv_mod

    parsed, error = srv_mod._parse_grouping_optional_fields({
        "trim_db": True,
        "client_latency_ms": 2.9,
        "left_delay_ms": "1.25",
        "right_delay_ms": False,
    })

    assert error is None
    assert parsed is not None
    assert parsed.trim_db == 1.0
    assert parsed.client_latency_ms == 2
    assert parsed.left_delay_ms == 1.25
    assert parsed.right_delay_ms == 0.0


@pytest.mark.parametrize(
    "field,value,message",
    (
        ("trim_db", "loud", "trim_db must be a number"),
        (
            "client_latency_ms",
            "soon",
            "client_latency_ms must be an integer",
        ),
        ("left_delay_ms", {}, "left_delay_ms must be a number"),
        ("right_delay_ms", None, "right_delay_ms must be a number"),
    ),
)
def test_grouping_set_optional_field_invalid_type_returns_exact_400(
    field: str,
    value: object,
    message: str,
    server_with_coordinator,
) -> None:
    base, _fake = server_with_coordinator

    status, response = _post(
        f"{base}/grouping/set",
        {"enabled": False, field: value},
    )

    assert status == 400
    assert response == {"error": message}


def test_grouping_set_trim_settable_validated_and_preserved(
    monkeypatch, server_with_coordinator,
):
    """trim_db: settable (validated attenuate-only), rejected when
    garbage or positive, and PRESERVED when omitted — bond/swap fan-outs
    never send it, so a calibrated balance survives role changes."""
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "follower", "channel": "right",
            "bond_id": "b", "leader_addr": "jts.local"}

    status, _ = _post(f"{base}/grouping/set", {**body, "trim_db": -2.5})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_TRIM_DB"] == "-2.5"

    status, resp = _post(f"{base}/grouping/set", {**body, "trim_db": 1.5})
    assert status == 400 and "must be between" in resp["error"]

    status, resp = _post(f"{base}/grouping/set", {**body, "trim_db": "loud"})
    assert status == 400 and "must be a number" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)  # omitted
    assert status == 200
    assert "JASPER_GROUPING_TRIM_DB" not in writes[-1]


def test_grouping_set_latency_and_delay_settable_validated_and_preserved(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "leader", "channel": "left",
            "bond_id": "b", "leader_addr": ""}

    status, _ = _post(
        f"{base}/grouping/set",
        {
            **body,
            "client_latency_ms": 11,
            "left_delay_ms": 1.25,
            "right_delay_ms": 0.5,
        },
    )
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_CLIENT_LATENCY_MS"] == "11"
    assert writes[-1]["JASPER_GROUPING_LEFT_DELAY_MS"] == "1.250"
    assert writes[-1]["JASPER_GROUPING_RIGHT_DELAY_MS"] == "0.500"

    status, resp = _post(
        f"{base}/grouping/set", {**body, "client_latency_ms": "soon"},
    )
    assert status == 400 and "client_latency_ms must be an integer" in resp["error"]

    status, resp = _post(
        f"{base}/grouping/set", {**body, "left_delay_ms": -0.1},
    )
    assert status == 400 and "LEFT_DELAY_MS" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)
    assert status == 200
    assert "JASPER_GROUPING_CLIENT_LATENCY_MS" not in writes[-1]
    assert "JASPER_GROUPING_LEFT_DELAY_MS" not in writes[-1]
    assert "JASPER_GROUPING_RIGHT_DELAY_MS" not in writes[-1]


def test_grouping_set_peer_roster_settable_preserved_and_cleared(
    monkeypatch, server_with_coordinator,
):
    """Bond roster fields: settable (validated private-IPv4), PRESERVED
    when omitted (swap/trim fan-outs never send them), and CLEARED by an
    explicit empty string (the bond flow clears non-leader members so a
    role flip can't leave a stale roster)."""
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "leader", "channel": "left",
            "bond_id": "b", "leader_addr": ""}

    status, _ = _post(f"{base}/grouping/set",
                      {**body, "peer_addr": "192.168.1.9",
                       "peer_name": "JTS3"})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_PEER_ADDR"] == "192.168.1.9"
    assert writes[-1]["JASPER_GROUPING_PEER_NAME"] == "JTS3"

    status, resp = _post(f"{base}/grouping/set",
                         {**body, "peer_addr": "8.8.8.8"})
    assert status == 400 and "private/loopback" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)  # omitted → preserved
    assert status == 200
    assert "JASPER_GROUPING_PEER_ADDR" not in writes[-1]
    assert "JASPER_GROUPING_PEER_NAME" not in writes[-1]

    status, _ = _post(f"{base}/grouping/set",
                      {**body, "peer_addr": "", "peer_name": ""})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_PEER_ADDR"] == ""
    assert writes[-1]["JASPER_GROUPING_PEER_NAME"] == ""


def test_grouping_set_roster_settable_preserved_and_validated(
    monkeypatch, server_with_coordinator,
):
    """Bond roster (full N-member list): a `roster` list of {addr,name,channel}
    persists the SERIALIZED JASPER_GROUPING_ROSTER; omitted → preserved (key
    absent); a bad member (non-IPv4 addr) → 400; a non-list value → 400."""
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "leader", "channel": "left",
            "bond_id": "b", "leader_addr": ""}

    # A roster list persists as the serialized env string (addr|name|channel
    # entries joined by ",").
    status, _ = _post(f"{base}/grouping/set", {
        **body, "roster": [
            {"addr": "192.168.1.7", "name": "Right", "channel": "right"},
            {"addr": "10.0.0.8", "name": "Den", "channel": "mono"},
        ],
    })
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_ROSTER"] == (
        "192.168.1.7|Right|right,10.0.0.8|Den|mono"
    )

    # A bad member (non-private/loopback IPv4 addr) → 400 (shared validator).
    status, resp = _post(f"{base}/grouping/set", {
        **body, "roster": [
            {"addr": "8.8.8.8", "name": "x", "channel": "right"},
        ],
    })
    assert status == 400 and "private/loopback" in resp["error"]

    # A non-list roster value → 400 before validate.
    status, resp = _post(f"{base}/grouping/set", {**body, "roster": "nope"})
    assert status == 400 and resp["error"] == "roster must be a list"

    # Omitted → preserved (key absent from this write).
    status, _ = _post(f"{base}/grouping/set", body)
    assert status == 200
    assert "JASPER_GROUPING_ROSTER" not in writes[-1]

    # Explicit empty list clears it (serializes to "").
    status, _ = _post(f"{base}/grouping/set", {**body, "roster": []})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_ROSTER"] == ""
