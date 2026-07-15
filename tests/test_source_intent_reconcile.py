# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free contract tests for the source lifecycle coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from jasper import source_intent
from jasper.music_sources import Source


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "source_intent.env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_target_status(
    status_path: str,
    env_path: str,
    source: Source,
    desired: str,
    *,
    result: str = "ok",
    effective: str = "off",
    reason: str = "",
    completed_monotonic_ns: int | None = None,
    fingerprint: str | None = None,
) -> None:
    text = Path(env_path).read_text(encoding="utf-8")
    source_intent._default_write_status(
        status_path,
        {
            "completed_monotonic_ns": (
                time.monotonic_ns()
                if completed_monotonic_ns is None
                else completed_monotonic_ns
            ),
            "intent_fingerprint": (
                source_intent._intent_fingerprint(text)
                if fingerprint is None
                else fingerprint
            ),
            "sources": {
                source.value: {
                    "desired": desired,
                    "effective": effective,
                    "result": result,
                    "reason": reason,
                },
            },
        },
    )


@dataclass
class _FakeHost:
    enabled: dict[str, bool] = field(default_factory=dict)
    active: dict[str, bool] = field(default_factory=dict)
    failed_units: set[str] = field(default_factory=set)
    allowed: bool = True
    usb_gadget_available: bool = True
    usb_role_pending_host: bool = False
    usb_audio: bool = False
    usb_direct: bool = False
    rfkill: source_intent.BluetoothRfkillState = field(
        default_factory=lambda: source_intent.BluetoothRfkillState(
            present=True,
            soft_blocked=False,
            hard_blocked=False,
        )
    )
    bluez: bool | None = False
    calls: list[tuple] = field(default_factory=list)
    fail: set[tuple[str, str]] = field(default_factory=set)
    available: set[str] = field(default_factory=set)

    def set_enabled(self, unit: str, enabled: bool) -> tuple[int, str]:
        verb = "enable" if enabled else "disable"
        self.calls.append((verb, unit))
        if (verb, unit) in self.fail:
            return 1, "injected failure"
        self.enabled[unit] = enabled
        return 0, ""

    def run_unit(self, unit: str, verb: str) -> tuple[int, str]:
        self.calls.append((verb, unit))
        if (verb, unit) in self.fail:
            return 1, "injected failure"
        if verb == "reset-failed":
            self.failed_units.discard(unit)
        elif unit == "jasper-usbgadget.service" and verb == "restart":
            self.usb_audio = bool(
                self.enabled.get("jasper-usbsink.service", False)
                and self.allowed
                and self.usb_direct
            )
        elif unit == "jasper-usbgadget.service" and verb == "stop":
            self.usb_audio = False
            self.active[unit] = False
        elif unit == source_intent._USB_COUPLING_UNIT:
            self.usb_direct = bool(
                self.enabled.get("jasper-usbsink.service", False) and self.allowed
            )
        else:
            self.active[unit] = verb == "start"
            if verb == "start":
                self.failed_units.discard(unit)
        return 0, ""

    def unit_enabled(self, unit: str) -> bool:
        return self.enabled.get(unit, False)

    def unit_active(self, unit: str) -> bool:
        return self.active.get(unit, False)

    def unit_failed(self, unit: str) -> bool:
        return unit in self.failed_units

    def set_rfkill(self, blocked: bool) -> tuple[int, str]:
        self.calls.append(("rfkill", "block" if blocked else "unblock"))
        if ("rfkill", "block" if blocked else "unblock") in self.fail:
            return 1, "injected failure"
        self.rfkill = source_intent.BluetoothRfkillState(
            present=self.rfkill.present,
            soft_blocked=blocked,
            hard_blocked=self.rfkill.hard_blocked,
        )
        return 0, ""

    def set_bluez(self, powered: bool) -> tuple[int, str]:
        self.calls.append(("bluez", powered))
        if ("bluez", str(powered)) in self.fail:
            return 1, "injected failure"
        self.bluez = powered
        return 0, ""

    def ops(self) -> source_intent.ReconcileOps:
        usb_role = source_intent.UsbPortRoleState(
            board_model="test",
            board_topology=(
                "separate_host_ports"
                if self.usb_gadget_available
                else "shared_otg_port"
            ),
            desired_role="peripheral" if self.usb_gadget_available else "host",
            configured_role="peripheral" if self.usb_gadget_available else "host",
            active_role=(
                "peripheral"
                if self.usb_gadget_available or self.usb_role_pending_host
                else "host"
            ),
            gadget_available=self.usb_gadget_available,
            reboot_required=self.usb_role_pending_host,
            reason=(
                "available"
                if self.usb_gadget_available
                else (
                    "role_change_pending_reboot"
                    if self.usb_role_pending_host
                    else "shared_otg_usb_output_requires_host"
                )
            ),
            decision_reason=(
                "dedicated_host_ports_leave_otg_available"
                if self.usb_gadget_available
                else "shared_otg_usb_output_requires_host"
            ),
            management_transport_available=(
                self.usb_gadget_available or self.usb_role_pending_host
            ),
        )
        return source_intent.ReconcileOps(
            set_enabled=self.set_enabled,
            run_unit=self.run_unit,
            unit_enabled=self.unit_enabled,
            unit_active=self.unit_active,
            unit_failed=self.unit_failed,
            unit_available=lambda unit: unit in self.available,
            local_sources_allowed=lambda: self.allowed,
            usb_port_role=lambda: usb_role,
            usb_audio_present=lambda: self.usb_audio,
            usb_direct_present=lambda: self.usb_direct,
            usb_direct_ready=lambda: self.usb_direct and self.usb_audio,
            rfkill_state=lambda: self.rfkill,
            set_rfkill_blocked=self.set_rfkill,
            bluez_powered=lambda: self.bluez,
            set_bluez_powered=self.set_bluez,
            settle=lambda _seconds: None,
        )


def _key(source: Source) -> str:
    return source_intent.intent_env_key(source)


def _bluetooth_runtime_units() -> set[str]:
    return {
        "bluealsa.service",
        "bluealsa-aplay.service",
        "bt-agent.service",
    }


def test_allowlist_and_legacy_keys_are_registry_derived():
    assert set(source_intent.source_intent_sources()) == {
        Source.AIRPLAY,
        Source.SPOTIFY,
        Source.BLUETOOTH,
        Source.USBSINK,
    }
    assert _key(Source.AIRPLAY) == "JASPER_SOURCE_INTENT_SHAIRPORT_SYNC_SERVICE"
    assert _key(Source.SPOTIFY) == "JASPER_SOURCE_INTENT_LIBRESPOT_SERVICE"
    assert _key(Source.USBSINK) == "JASPER_SOURCE_INTENT_JASPER_USBSINK_SERVICE"
    assert _key(Source.BLUETOOTH) == "JASPER_BLUETOOTH_SOURCE_INTENT"
    # Existing unit-string callers remain byte-for-byte compatible.
    assert source_intent.intent_env_key("shairport-sync.service") == _key(
        Source.AIRPLAY
    )


def test_read_source_intents_fills_defaults_and_applies_overrides(tmp_path):
    missing = tmp_path / "missing.env"
    assert source_intent.read_source_intents(str(missing)) == {
        Source.AIRPLAY: True,
        Source.SPOTIFY: True,
        Source.BLUETOOTH: True,
        Source.USBSINK: False,
    }
    env = _write(
        tmp_path,
        f"{_key(Source.AIRPLAY)}=disabled\n{_key(Source.BLUETOOTH)}=disabled\n",
    )
    intents = source_intent.read_source_intents(env)
    assert intents[Source.AIRPLAY] is False
    assert intents[Source.BLUETOOTH] is False
    assert intents[Source.SPOTIFY] is True
    assert source_intent.source_intent_enabled(Source.USBSINK, env) is False


def test_request_source_intent_writes_fixed_key_then_kicks(tmp_path):
    calls = []
    status_path = str(tmp_path / "status.json")

    def writer(path, updates):
        calls.append(("write", path, dict(updates)))
        source_intent._default_write_intent(path, updates)

    def kicker():
        calls.append(("kick",))
        _write_target_status(
            status_path,
            path,
            Source.BLUETOOTH,
            "disabled",
        )
        return {"ok": True}

    path = str(tmp_path / "intent.env")
    source_intent.request_source_intent(
        Source.BLUETOOTH,
        False,
        env_path=path,
        status_path=status_path,
        writer=writer,
        kicker=kicker,
    )
    assert calls == [
        ("write", path, {"JASPER_BLUETOOTH_SOURCE_INTENT": "disabled"}),
        ("kick",),
    ]


def test_source_request_and_reconcile_locks_are_shared_group_writable(tmp_path):
    path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")

    def kicker():
        _write_target_status(
            status_path,
            path,
            Source.BLUETOOTH,
            "disabled",
        )
        return {"ok": True}

    source_intent.request_source_intent(
        Source.BLUETOOTH,
        False,
        env_path=path,
        status_path=status_path,
        kicker=kicker,
    )
    assert os.stat(f"{path}.request.lock").st_mode & 0o777 == 0o660

    host = _FakeHost()
    assert source_intent.reconcile(env_path=path, ops=host.ops()) == 0
    assert os.stat(f"{path}.reconcile.lock").st_mode & 0o777 == 0o660


def test_default_writer_publishes_env_and_inner_lock_for_both_web_owners(
    monkeypatch,
):
    from jasper import atomic_io

    calls = []
    monkeypatch.setattr(
        atomic_io,
        "locked_update_env_file",
        lambda path, updates, **kwargs: calls.append((path, updates, kwargs)),
    )

    source_intent._default_write_intent("/var/lib/jasper/source_intent.env", {"K": "V"})

    assert calls == [
        (
            "/var/lib/jasper/source_intent.env",
            {"K": "V"},
            {
                "mode": 0o660,
                "group_from_parent": True,
                "lock_mode": 0o660,
                "max_bytes": source_intent._MAX_INTENT_BYTES,
                "lock_timeout_sec": source_intent._REQUEST_LOCK_TIMEOUT_SEC,
            },
        )
    ]


def test_blocking_unit_waits_match_owner_oneshot_timeouts(monkeypatch):
    """Client waits outlast source service and owner-oneshot contracts."""
    import subprocess as sp

    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["timeout"]))
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(source_intent.subprocess, "run", fake_run)
    assert source_intent._run_unit_action("librespot.service", "start") == (0, "")
    assert source_intent._run_unit_action("shairport-sync.service", "start") == (
        0,
        "",
    )
    assert source_intent._run_unit_action("jasper-usbsink.service", "start") == (
        0,
        "",
    )
    assert source_intent._run_unit_action("jasper-usbgadget.service", "restart") == (
        0,
        "",
    )
    assert source_intent._run_unit_action("bt-agent.service", "stop") == (0, "")
    assert source_intent._run_unit_action(
        source_intent._ACCESSORY_RECONCILE_UNIT,
        "start",
    ) == (0, "")
    assert source_intent._run_unit_action(
        source_intent._USB_COUPLING_UNIT,
        "start",
    ) == (0, "")
    assert calls == [
        (["systemctl", "start", "librespot.service"], 3.0),
        (
            ["systemctl", "start", "shairport-sync.service"],
            source_intent._unit_action_timeout_sec("shairport-sync.service", "start"),
        ),
        (
            ["systemctl", "start", "jasper-usbsink.service"],
            source_intent._unit_action_timeout_sec("jasper-usbsink.service", "start"),
        ),
        (["systemctl", "restart", "jasper-usbgadget.service"], 11.0),
        (["systemctl", "stop", "bt-agent.service"], 11.0),
        (
            ["systemctl", "start", source_intent._ACCESSORY_RECONCILE_UNIT],
            65.0,
        ),
        (["systemctl", "start", source_intent._USB_COUPLING_UNIT], 125.0),
    ]


def test_web_broker_wait_outlasts_complete_source_reconcile(monkeypatch):
    from jasper.control import restart_broker

    calls = []
    monkeypatch.setattr(
        restart_broker,
        "manage_units",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    assert source_intent.kick_source_reconcile() == {"ok": True}
    assert calls == [
        (
            (source_intent.RECONCILE_UNIT,),
            {
                "verb": "start",
                "reason": "source enable/disable",
                "no_block": False,
                "timeout": source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS,
            },
        )
    ]


def test_request_source_intent_keeps_written_intent_when_kick_fails(tmp_path):
    written = []
    env_path = str(tmp_path / "intent.env")

    def writer(path, updates):
        written.append(dict(updates))
        source_intent._default_write_intent(path, updates)

    with pytest.raises(RuntimeError, match="could not apply bluetooth disabled"):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=str(tmp_path / "missing-status.json"),
            writer=writer,
            kicker=lambda: {"ok": False, "error": "coordinator failed"},
        )
    assert written == [{"JASPER_BLUETOOTH_SOURCE_INTENT": "disabled"}]


def test_request_succeeds_when_target_did_but_sibling_failed(tmp_path, caplog):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
        )
        return {"ok": False, "error": "spotify failed"}

    with caplog.at_level("WARNING"):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 1
    assert "event=source.intent_sibling_failure" in caplog.text
    assert "spotify failed" in caplog.text


def test_request_fails_when_fresh_target_outcome_failed(tmp_path):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
            result="failed",
            effective="degraded",
            reason="rfkill failed",
        )
        return {"ok": False, "error": "aggregate failed"}

    with pytest.raises(
        RuntimeError,
        match="aggregate=aggregate failed.*target effective=degraded failed: rfkill failed",
    ):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 1


def test_request_retries_once_after_stale_join_then_accepts_fresh_pass(tmp_path):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        _write_target_status(
            status_path,
            env_path,
            Source.BLUETOOTH,
            "disabled",
            completed_monotonic_ns=0 if kicks == 1 else None,
        )
        return {"ok": True}

    source_intent.request_source_intent(
        Source.BLUETOOTH,
        False,
        env_path=env_path,
        status_path=status_path,
        kicker=kicker,
    )

    assert kicks == 2


@pytest.mark.parametrize(
    ("status_shape", "detail"),
    [
        ("stale", "completion status is stale"),
        ("malformed", "completion status is unreadable"),
        ("wrong_fingerprint", "completion status intent does not match"),
    ],
)
def test_request_refuses_untrusted_completion_status(tmp_path, status_shape, detail):
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")
    kicks = 0

    def kicker():
        nonlocal kicks
        kicks += 1
        if status_shape == "malformed":
            Path(status_path).write_text("not-json", encoding="utf-8")
        else:
            _write_target_status(
                status_path,
                env_path,
                Source.BLUETOOTH,
                "disabled",
                completed_monotonic_ns=(0 if status_shape == "stale" else None),
                fingerprint=("0" * 64 if status_shape == "wrong_fingerprint" else None),
            )
        return {"ok": True}

    with pytest.raises(RuntimeError, match=detail):
        source_intent.request_source_intent(
            Source.BLUETOOTH,
            False,
            env_path=env_path,
            status_path=status_path,
            kicker=kicker,
        )

    assert kicks == 2


def test_request_source_intent_serializes_write_and_apply_across_callers(tmp_path):
    """A second writer cannot join an already-running apply with newer state."""
    first_apply_entered = threading.Event()
    release_first_apply = threading.Event()
    second_write_seen = threading.Event()
    calls: list[tuple[str, str]] = []
    env_path = str(tmp_path / "intent.env")
    status_path = str(tmp_path / "status.json")

    def request(name: str, source: Source) -> None:
        def writer(path, updates):
            calls.append((name, "write"))
            source_intent._default_write_intent(path, updates)
            if name == "second":
                second_write_seen.set()

        def kicker():
            calls.append((name, "apply"))
            if name == "first":
                first_apply_entered.set()
                assert release_first_apply.wait(timeout=2)
            _write_target_status(
                status_path,
                env_path,
                source,
                "disabled",
            )
            return {"ok": True}

        source_intent.request_source_intent(
            source,
            False,
            env_path=env_path,
            status_path=status_path,
            writer=writer,
            kicker=kicker,
        )

    first = threading.Thread(target=request, args=("first", Source.AIRPLAY))
    second = threading.Thread(target=request, args=("second", Source.SPOTIFY))
    first.start()
    assert first_apply_entered.wait(timeout=2)
    second.start()
    assert not second_write_seen.wait(timeout=0.1)
    release_first_apply.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [
        ("first", "write"),
        ("first", "apply"),
        ("second", "write"),
        ("second", "apply"),
    ]


def test_direct_and_systemd_reconcile_invocations_serialize_before_read(
    tmp_path,
    monkeypatch,
):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls: list[str] = []

    def reconcile_once(**_kwargs):
        name = threading.current_thread().name
        calls.append(name)
        if name == "first":
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return 0

    monkeypatch.setattr(source_intent, "_reconcile_once", reconcile_once)
    path = str(tmp_path / "intent.env")
    first = threading.Thread(
        target=source_intent.reconcile,
        kwargs={"env_path": path},
        name="first",
    )
    second = threading.Thread(
        target=source_intent.reconcile,
        kwargs={"env_path": path},
        name="second",
    )
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert calls == ["first", "second"]


def test_reconcile_missing_file_converges_all_shipped_defaults(tmp_path):
    host = _FakeHost()
    rc = source_intent.reconcile(
        env_path=str(tmp_path / "missing.env"),
        ops=host.ops(),
    )
    assert rc == 0
    assert host.enabled["shairport-sync.service"] is True
    assert host.enabled["nqptp.service"] is True
    assert host.enabled["librespot.service"] is True
    assert host.enabled.get("jasper-usbsink.service", False) is False
    assert all(host.enabled[unit] for unit in _bluetooth_runtime_units())
    assert host.active["shairport-sync.service"] is True
    assert host.active["nqptp.service"] is True
    assert host.active["librespot.service"] is True
    assert host.active["bluetooth.service"] is True
    assert all(host.active[unit] for unit in _bluetooth_runtime_units())
    assert host.usb_audio is False


def test_production_reconcile_publishes_atomic_per_source_outcomes(tmp_path):
    host = _FakeHost()
    env_path = str(tmp_path / "missing.env")
    status_path = tmp_path / "status.json"

    assert (
        source_intent.reconcile(
            env_path=env_path,
            ops=host.ops(),
            status_path=str(status_path),
        )
        == 0
    )

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["completed_monotonic_ns"] > 0
    assert payload["intent_fingerprint"] == source_intent._intent_fingerprint("")
    assert set(payload["sources"]) == {
        source.value for source in source_intent.source_intent_sources()
    }
    assert payload["sources"]["bluetooth"] == {
        "desired": "enabled",
        "effective": "on",
        "result": "ok",
        "reason": "",
    }
    assert payload["sources"]["usbsink"] == {
        "desired": "disabled",
        "effective": "off",
        "result": "ok",
        "reason": "",
    }
    assert status_path.stat().st_mode & 0o777 == 0o644


def test_production_status_reader_rejects_writable_parent(tmp_path, monkeypatch):
    env_path = _write(tmp_path, f"{_key(Source.BLUETOOTH)}=disabled\n")
    status_path = str(tmp_path / "status.json")
    _write_target_status(
        status_path,
        env_path,
        Source.BLUETOOTH,
        "disabled",
    )
    monkeypatch.setattr(source_intent, "SOURCE_STATUS_PATH", status_path)
    real_lstat = os.lstat

    def unsafe_parent(path):
        observed = real_lstat(path)
        mode = observed.st_mode
        if os.fspath(path) == os.fspath(tmp_path):
            mode |= 0o022
        return SimpleNamespace(st_mode=mode, st_uid=0)

    monkeypatch.setattr(source_intent.os, "lstat", unsafe_parent)
    result = source_intent._read_target_status(
        path=status_path,
        source=Source.BLUETOOTH,
        desired="disabled",
        intent_fingerprint=source_intent._intent_fingerprint(
            Path(env_path).read_text(encoding="utf-8")
        ),
        not_before_monotonic_ns=0,
    )

    assert result.exact is False
    assert result.detail == "completion status ownership is unsafe"


def test_direct_reconcile_is_status_io_free_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        source_intent,
        "_default_write_status",
        lambda *_args, **_kwargs: pytest.fail("unexpected production status write"),
    )

    assert (
        source_intent.reconcile(
            env_path=str(tmp_path / "missing.env"),
            ops=_FakeHost().ops(),
        )
        == 0
    )


def test_reconcile_is_idempotent_after_convergence(tmp_path):
    host = _FakeHost()
    env = str(tmp_path / "missing.env")
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    host.calls.clear()
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    # USB Off still invokes its canonical owner so stale persisted fanin.env is
    # repaired even when the live daemon is absent; the owner itself is
    # idempotent and performs no restart when its files already match.
    assert host.calls == [("start", source_intent._USB_COUPLING_UNIT)]


def test_reconcile_rejects_unknown_prefixed_key_but_applies_valid_sources(
    tmp_path,
    caplog,
):
    host = _FakeHost()
    env = _write(
        tmp_path,
        "JASPER_SOURCE_INTENT_SSHD_SERVICE=disabled\n"
        f"{_key(Source.AIRPLAY)}=disabled\n",
    )
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(env_path=env, ops=host.ops())
    assert rc == 1
    assert all("sshd" not in str(call).lower() for call in host.calls)
    assert host.enabled.get("shairport-sync.service", False) is False
    assert host.enabled["librespot.service"] is True
    assert "event=source_intent.rejected_unit" in caplog.text


def test_bad_value_tears_down_that_source_without_blocking_others(
    tmp_path,
    caplog,
):
    """Malformed recognized intent is loud Off, never leave-running."""

    host = _FakeHost(
        enabled={
            "shairport-sync.service": True,
            "nqptp.service": True,
        },
        active={
            "shairport-sync.service": True,
            "nqptp.service": True,
        },
    )
    env = _write(tmp_path, f"{_key(Source.AIRPLAY)}=maybe\n")
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(env_path=env, ops=host.ops())
    assert rc == 1
    assert host.enabled["shairport-sync.service"] is False
    assert host.enabled["nqptp.service"] is False
    assert host.active["shairport-sync.service"] is False
    assert host.active["nqptp.service"] is False
    assert host.active["librespot.service"] is True
    assert "event=source_intent.bad_value" in caplog.text
    assert "event=source.reconcile source=airplay desired=invalid" in caplog.text
    assert "reason=invalid_intent_fail_closed" in caplog.text


def test_public_reader_fails_strictly_on_bad_or_unknown_keys(tmp_path):
    bad_value = _write(tmp_path, f"{_key(Source.AIRPLAY)}=maybe\n")
    with pytest.raises(RuntimeError, match="invalid intent value"):
        source_intent.read_source_intents(bad_value)
    unknown = _write(tmp_path, "JASPER_SOURCE_INTENT_SSHD_SERVICE=enabled\n")
    with pytest.raises(RuntimeError, match="unrecognized source intent key"):
        source_intent.read_source_intents(unknown)


def test_per_source_reader_fails_only_the_affected_source(tmp_path):
    env = _write(
        tmp_path,
        f"{_key(Source.AIRPLAY)}=maybe\n"
        "JASPER_SOURCE_INTENT_SSHD_SERVICE=enabled\n"
        f"{_key(Source.SPOTIFY)}=enabled\n",
    )

    with pytest.raises(RuntimeError, match="invalid intent value for airplay"):
        source_intent.source_intent_enabled(Source.AIRPLAY, env)
    assert source_intent.source_intent_enabled(Source.SPOTIFY, env) is True


def test_systemd_source_failure_is_isolated_and_observable(tmp_path, caplog):
    host = _FakeHost(fail={("enable", "shairport-sync.service")})
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(
            env_path=str(tmp_path / "missing.env"),
            ops=host.ops(),
        )
    assert rc == 1
    assert host.active["librespot.service"] is True
    assert "event=source.reconcile source=airplay" in caplog.text
    assert "result=failed" in caplog.text


def test_declared_intent_unit_selects_ordinary_systemd_applier(monkeypatch):
    """An ordinary source declaration needs no second enum dispatch edit."""
    declared_source = object()
    lifecycle = replace(
        source_intent.local_source_lifecycle(Source.SPOTIFY),
        source=declared_source,
        intent_unit="declared-renderer.service",
        runtime_units=("declared-renderer.service",),
    )
    monkeypatch.setattr(
        source_intent,
        "local_source_lifecycle",
        lambda source: lifecycle if source is declared_source else None,
    )
    host = _FakeHost()

    assert (
        source_intent._apply_source(
            declared_source,  # type: ignore[arg-type]
            True,
            True,
            host.ops(),
        )
        == "on"
    )
    assert host.calls == [
        ("enable", "declared-renderer.service"),
        ("start", "declared-renderer.service"),
    ]


def test_disabling_stale_enabled_unit_cancels_queued_boot_start(tmp_path):
    host = _FakeHost(
        enabled={
            "shairport-sync.service": True,
            "nqptp.service": True,
        },
        # An activating unit is reported as an indeterminate state by the real
        # probe, which forces a stop. Active=True models the same safety path
        # without teaching this fake a third state.
        active={
            "shairport-sync.service": True,
            "nqptp.service": True,
        },
    )
    env = _write(tmp_path, f"{_key(Source.AIRPLAY)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    assert ("stop", "shairport-sync.service") in host.calls
    assert ("stop", "nqptp.service") in host.calls


def test_ordinary_source_failed_to_off_resets_terminal_state(tmp_path):
    unit = "shairport-sync.service"
    host = _FakeHost(failed_units={unit})
    env = _write(tmp_path, f"{_key(Source.AIRPLAY)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    assert ("reset-failed", unit) in host.calls
    assert host.unit_active(unit) is False
    assert host.unit_failed(unit) is False


def test_usb_enable_arms_direct_lane_before_advertising_audio(tmp_path):
    host = _FakeHost()
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    usb_calls = [
        call
        for call in host.calls
        if any(
            "usbsink" in str(part)
            or "usbgadget" in str(part)
            or "coupling" in str(part)
            for part in call
        )
    ]
    assert usb_calls == [
        ("enable", "jasper-usbsink.service"),
        ("start", source_intent._USB_COUPLING_UNIT),
        ("restart", "jasper-usbgadget.service"),
        ("start", "jasper-usbsink.service"),
    ]
    assert host.usb_direct is True
    assert host.usb_audio is True
    assert host.active["jasper-usbsink.service"] is True


def test_usb_desired_on_parks_and_restores_through_one_source_owner(tmp_path):
    """A follower role change preserves intent and the solo replay restores
    direct capture before UAC2 advertisement and standby liveness."""
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        usb_audio=True,
        usb_direct=True,
        allowed=False,
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    assert host.enabled["jasper-usbsink.service"] is True
    assert host.active["jasper-usbsink.service"] is False
    assert host.usb_audio is False
    assert host.usb_direct is False

    host.allowed = True
    host.calls.clear()
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    usb_calls = [
        call
        for call in host.calls
        if any(
            token in str(part)
            for part in call
            for token in ("usbsink", "usbgadget", "coupling")
        )
    ]
    assert usb_calls == [
        ("start", source_intent._USB_COUPLING_UNIT),
        ("restart", "jasper-usbgadget.service"),
        ("start", "jasper-usbsink.service"),
    ]
    assert host.usb_direct is True
    assert host.usb_audio is True
    assert host.active["jasper-usbsink.service"] is True


def test_usb_enable_withdraws_stale_uac2_before_arming_direct_lane(tmp_path):
    host = _FakeHost(usb_audio=True, usb_direct=False)
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    usb_calls = [
        call
        for call in host.calls
        if any(
            token in str(part)
            for part in call
            for token in ("usbsink", "usbgadget", "coupling")
        )
    ]
    assert usb_calls == [
        ("enable", "jasper-usbsink.service"),
        ("restart", "jasper-usbgadget.service"),
        ("start", source_intent._USB_COUPLING_UNIT),
        ("restart", "jasper-usbgadget.service"),
        ("start", "jasper-usbsink.service"),
    ]
    assert host.usb_audio is True
    assert host.usb_direct is True


def test_usb_disable_stops_then_recomposes_without_dropping_network(tmp_path):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        usb_audio=True,
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=disabled\n")
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    usb_calls = [
        call
        for call in host.calls
        if any(
            "usbsink" in str(part)
            or "usbgadget" in str(part)
            or "coupling" in str(part)
            for part in call
        )
    ]
    assert usb_calls == [
        ("disable", "jasper-usbsink.service"),
        ("stop", "jasper-usbsink.service"),
        ("restart", "jasper-usbgadget.service"),
        ("start", source_intent._USB_COUPLING_UNIT),
    ]
    assert ("stop", "jasper-usbgadget.service") not in host.calls
    assert host.usb_audio is False


def test_usb_desired_on_is_unavailable_when_output_owns_shared_port(tmp_path):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={
            "jasper-usbsink.service": True,
            "jasper-usbgadget.service": True,
        },
        usb_gadget_available=False,
        usb_audio=True,
        usb_direct=True,
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")
    status = tmp_path / "status.json"

    assert source_intent.reconcile(
        env_path=env,
        ops=host.ops(),
        status_path=str(status),
    ) == 0

    assert source_intent.source_intent_enabled(Source.USBSINK, env_path=env) is True
    assert host.enabled["jasper-usbsink.service"] is False
    assert host.active["jasper-usbsink.service"] is False
    assert host.usb_audio is False
    assert host.usb_direct is False
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["sources"]["usbsink"] == {
        "desired": "enabled",
        "effective": "unavailable",
        "result": "ok",
        "reason": "shared_otg_usb_output_requires_host",
    }


def test_usb_desired_off_is_clean_when_shared_port_is_unavailable(tmp_path):
    host = _FakeHost(usb_gadget_available=False)
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=disabled\n")
    status = tmp_path / "status.json"

    assert source_intent.reconcile(
        env_path=env,
        ops=host.ops(),
        status_path=str(status),
    ) == 0
    assert host.enabled.get("jasper-usbsink.service", False) is False
    assert host.usb_audio is False
    assert host.usb_direct is False
    assert json.loads(status.read_text())["sources"]["usbsink"]["effective"] == "off"


def test_usb_follower_parking_remains_distinct_from_hardware_unavailable(
    tmp_path,
):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        allowed=False,
        usb_gadget_available=False,
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")
    status = tmp_path / "status.json"

    assert source_intent.reconcile(
        env_path=env,
        ops=host.ops(),
        status_path=str(status),
    ) == 0

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["sources"]["usbsink"]["effective"] == "parked"


def test_pending_host_reboot_withdraws_audio_but_retains_management_gadget(
    tmp_path,
):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={
            "jasper-usbsink.service": True,
            "jasper-usbgadget.service": True,
        },
        usb_gadget_available=False,
        usb_role_pending_host=True,
        usb_audio=True,
        usb_direct=True,
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=enabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    assert host.active["jasper-usbgadget.service"] is True
    assert host.usb_audio is False
    assert host.usb_direct is False
    assert ("restart", "jasper-usbgadget.service") in host.calls


def test_usb_failed_to_off_resets_terminal_state(tmp_path):
    unit = "jasper-usbsink.service"
    host = _FakeHost(
        enabled={unit: True},
        failed_units={unit},
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    assert ("reset-failed", unit) in host.calls
    assert host.unit_active(unit) is False
    assert host.unit_failed(unit) is False


def test_usb_disable_recompose_failure_stops_gadget_before_disarming(tmp_path):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        usb_audio=True,
        usb_direct=True,
        fail={("restart", "jasper-usbgadget.service")},
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 1

    assert host.usb_audio is False
    assert host.usb_direct is False
    restart = host.calls.index(("restart", "jasper-usbgadget.service"))
    stop = host.calls.index(("stop", "jasper-usbgadget.service"))
    disarm = host.calls.index(("start", source_intent._USB_COUPLING_UNIT))
    assert restart < stop < disarm


@pytest.mark.parametrize(
    ("intent", "allowed"),
    [
        ("disabled", True),
        ("enabled", False),
    ],
)
def test_usb_off_or_park_keeps_direct_armed_if_uac2_cannot_be_withdrawn(
    tmp_path,
    intent,
    allowed,
):
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        allowed=allowed,
        usb_audio=True,
        usb_direct=True,
        fail={
            ("restart", "jasper-usbgadget.service"),
            ("stop", "jasper-usbgadget.service"),
        },
    )
    env = _write(tmp_path, f"{_key(Source.USBSINK)}={intent}\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 1

    assert host.usb_audio is True
    assert host.usb_direct is True
    assert ("start", source_intent._USB_COUPLING_UNIT) not in host.calls


def test_usb_health_fallback_withdraws_uac2_before_direct_disarm():
    """The health watcher delegates the host-visible phase to this owner.

    UAC2 is recomposed away while the direct consumer is still present; the
    coupling watcher may disarm that consumer only after this returns success.
    The ordinary restart preserves the composite gadget/NCM owner.
    """

    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        usb_audio=True,
        usb_direct=True,
    )

    ok, detail = source_intent.withdraw_usbsink_audio_for_fallback(ops=host.ops())

    assert ok is True
    assert detail == ""
    assert host.usb_audio is False
    assert host.usb_direct is True
    assert host.enabled["jasper-usbsink.service"] is False
    assert host.active["jasper-usbsink.service"] is False
    assert host.calls == [
        ("stop", "jasper-usbsink.service"),
        ("disable", "jasper-usbsink.service"),
        ("restart", "jasper-usbgadget.service"),
    ]
    assert ("stop", "jasper-usbgadget.service") not in host.calls


def test_usb_health_fallback_recompose_failure_stops_gadget_fail_closed():
    host = _FakeHost(
        enabled={"jasper-usbsink.service": True},
        active={"jasper-usbsink.service": True},
        usb_audio=True,
        usb_direct=True,
        fail={("restart", "jasper-usbgadget.service")},
    )

    ok, detail = source_intent.withdraw_usbsink_audio_for_fallback(ops=host.ops())

    assert ok is False
    assert "injected failure" in detail
    assert host.usb_audio is False
    assert host.usb_direct is True
    assert host.calls[-1] == ("stop", "jasper-usbgadget.service")


def test_converged_usb_off_still_repairs_persisted_coupling_state(tmp_path):
    """A missing live DIRECT lane cannot prove stale fanin.env is disarmed."""

    host = _FakeHost()
    assert (
        source_intent.reconcile(env_path=str(tmp_path / "missing.env"), ops=host.ops())
        == 0
    )
    host.calls.clear()
    env = _write(tmp_path, f"{_key(Source.AIRPLAY)}=disabled\n")
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    assert all("usbgadget" not in str(call) for call in host.calls)
    assert host.usb_direct is False
    assert host.calls.count(("start", source_intent._USB_COUPLING_UNIT)) == 1


def test_bluetooth_enable_clears_soft_block_before_power_and_dependents(tmp_path):
    host = _FakeHost(
        rfkill=source_intent.BluetoothRfkillState(True, True, False),
        bluez=False,
    )
    assert (
        source_intent.reconcile(env_path=str(tmp_path / "missing.env"), ops=host.ops())
        == 0
    )
    bt_calls = [
        call
        for call in host.calls
        if call[0] in {"rfkill", "bluez"}
        or (
            len(call) > 1
            and call[1] in _bluetooth_runtime_units() | {"bluetooth.service"}
        )
    ]
    assert bt_calls == [
        ("enable", "bluealsa.service"),
        ("enable", "bluealsa-aplay.service"),
        ("enable", "bt-agent.service"),
        ("start", "bluetooth.service"),
        ("rfkill", "unblock"),
        ("bluez", True),
        ("start", "bluealsa.service"),
        ("start", "bluealsa-aplay.service"),
        ("start", "bt-agent.service"),
    ]


def test_bluetooth_enable_waits_for_late_rfkill_registration(tmp_path):
    host = _FakeHost(
        rfkill=source_intent.BluetoothRfkillState(False, False, False),
        bluez=False,
    )
    probes = 0

    def rfkill_state():
        nonlocal probes
        probes += 1
        if probes < 3:
            return source_intent.BluetoothRfkillState(False, False, False)
        return source_intent.BluetoothRfkillState(True, False, False)

    ops = replace(host.ops(), rfkill_state=rfkill_state)
    assert (
        source_intent.reconcile(
            env_path=str(tmp_path / "missing.env"),
            ops=ops,
        )
        == 0
    )
    assert probes >= 3
    assert host.bluez is True


def test_bluetooth_disable_stops_disables_powers_down_and_blocks(tmp_path):
    units = _bluetooth_runtime_units()
    host = _FakeHost(
        enabled={unit: True for unit in units},
        active={unit: True for unit in units},
        bluez=True,
    )
    env = _write(tmp_path, f"{_key(Source.BLUETOOTH)}=disabled\n")
    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0
    bt_calls = [
        call
        for call in host.calls
        if call[0] in {"rfkill", "bluez"} or (len(call) > 1 and call[1] in units)
    ]
    assert bt_calls == [
        ("disable", "bluealsa.service"),
        ("disable", "bluealsa-aplay.service"),
        ("disable", "bt-agent.service"),
        ("stop", "bt-agent.service"),
        ("stop", "bluealsa-aplay.service"),
        ("stop", "bluealsa.service"),
        ("bluez", False),
        ("rfkill", "block"),
    ]
    assert all(host.enabled[unit] is False for unit in units)
    assert all(host.active[unit] is False for unit in units)


def test_bluetooth_failed_to_off_resets_terminal_state(tmp_path):
    unit = "bt-agent.service"
    host = _FakeHost(
        enabled={member: True for member in _bluetooth_runtime_units()},
        failed_units={unit},
        bluez=False,
    )
    env = _write(tmp_path, f"{_key(Source.BLUETOOTH)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 0

    assert ("reset-failed", unit) in host.calls
    assert host.unit_active(unit) is False
    assert host.unit_failed(unit) is False


def test_bluetooth_disable_attempts_rfkill_after_service_and_power_failures(tmp_path):
    units = _bluetooth_runtime_units()
    host = _FakeHost(
        enabled={unit: True for unit in units},
        active={unit: True for unit in units},
        bluez=True,
        fail={
            ("stop", "bt-agent.service"),
            ("bluez", "False"),
        },
    )
    env = _write(tmp_path, f"{_key(Source.BLUETOOTH)}=disabled\n")

    assert source_intent.reconcile(env_path=env, ops=host.ops()) == 1

    assert ("stop", "bluealsa-aplay.service") in host.calls
    assert ("stop", "bluealsa.service") in host.calls
    assert ("rfkill", "block") in host.calls
    assert host.rfkill.soft_blocked is True


def test_bluetooth_toggle_delegates_optional_accessories_to_their_owner(tmp_path):
    host = _FakeHost(available={source_intent._ACCESSORY_RECONCILE_UNIT})
    assert (
        source_intent.reconcile(env_path=str(tmp_path / "missing.env"), ops=host.ops())
        == 0
    )
    assert (
        host.calls.count(
            (
                "start",
                source_intent._ACCESSORY_RECONCILE_UNIT,
            )
        )
        == 2
    )


def test_converged_bluetooth_still_self_heals_accessory_owner(tmp_path):
    units = _bluetooth_runtime_units()
    host = _FakeHost(
        enabled={unit: True for unit in units},
        active={unit: True for unit in units},
        bluez=True,
        available={source_intent._ACCESSORY_RECONCILE_UNIT},
    )
    assert (
        source_intent.reconcile(
            env_path=str(tmp_path / "missing.env"),
            ops=host.ops(),
        )
        == 0
    )
    assert (
        host.calls.count(
            (
                "start",
                source_intent._ACCESSORY_RECONCILE_UNIT,
            )
        )
        == 2
    )


def test_parked_bluetooth_keeps_intent_enabled_without_touching_radio(tmp_path):
    units = _bluetooth_runtime_units()
    host = _FakeHost(
        allowed=False,
        enabled={unit: False for unit in units},
        active={unit: True for unit in units},
        bluez=True,
    )
    assert (
        source_intent.reconcile(env_path=str(tmp_path / "missing.env"), ops=host.ops())
        == 0
    )
    assert all(host.enabled[unit] is True for unit in units)
    assert all(host.active[unit] is False for unit in units)
    assert host.bluez is True
    assert host.rfkill.soft_blocked is False
    assert not any(call[0] in {"rfkill", "bluez"} for call in host.calls)


def test_hard_block_fails_bluetooth_without_blocking_other_sources(
    tmp_path,
    caplog,
):
    host = _FakeHost(
        rfkill=source_intent.BluetoothRfkillState(True, False, True),
    )
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(
            env_path=str(tmp_path / "missing.env"), ops=host.ops()
        )
    assert rc == 1
    assert host.active["shairport-sync.service"] is True
    assert host.active["librespot.service"] is True
    assert "event=source.reconcile source=bluetooth" in caplog.text
    assert "hardware-blocked" in caplog.text


def test_stable_source_reconcile_log_emitted_for_every_source(tmp_path, caplog):
    host = _FakeHost()
    with caplog.at_level("INFO"):
        assert (
            source_intent.reconcile(
                env_path=str(tmp_path / "missing.env"), ops=host.ops()
            )
            == 0
        )
    for source in source_intent.source_intent_sources():
        assert f"event=source.reconcile source={source.value}" in caplog.text
    assert caplog.text.count("event=source.reconcile source=") == 4


def test_oversized_utf8_intent_is_rejected_by_bytes(tmp_path, caplog):
    path = tmp_path / "source_intent.env"
    # Each snowman is three UTF-8 bytes. Character-count capping would accept
    # this; the root boundary must cap actual bytes read from disk.
    path.write_bytes("☃".encode() * (source_intent._MAX_INTENT_BYTES // 3 + 1))
    host = _FakeHost()
    with caplog.at_level("WARNING"):
        rc = source_intent.reconcile(env_path=str(path), ops=host.ops())
    assert rc == 1
    assert host.calls == []
    assert "event=source_intent.read_failed" in caplog.text


def test_invalid_utf8_intent_is_rejected_before_hardware_actions(tmp_path):
    path = tmp_path / "source_intent.env"
    path.write_bytes(b"\xff")
    host = _FakeHost()
    assert source_intent.reconcile(env_path=str(path), ops=host.ops()) == 1
    assert host.calls == []


def test_reconcile_unit_matches_broker_start_only_grant():
    from jasper.control.restart_broker import START_ONLY_UNITS

    assert source_intent.RECONCILE_UNIT in START_ONLY_UNITS


def test_main_returns_reconcile_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(source_intent, "reconcile", lambda **kwargs: 1)
    assert (
        source_intent.main(
            [
                "--status-path",
                str(tmp_path / "source-intent" / "status.json"),
            ]
        )
        == 1
    )
